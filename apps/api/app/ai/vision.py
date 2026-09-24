"""Image embedding + captioning for person recognition (Azure AI Foundry).

Two capabilities of one Azure AI Services ("AI Foundry") account are used,
and they are deliberately modelled as two small, independently-degrading
providers rather than one:

``ImageEmbedder``
    Turns a cropped person photo into a vector. Two photos of the *same*
    person taken minutes or days apart land close together in this space,
    which is what makes "this is the same person who came yesterday"
    possible at all. Azure's multimodal embeddings model does this properly;
    the local fallback cannot, and says so (see :class:`LocalImageEmbedder`).

``ImageCaptioner``
    Describes the crop in one plain sentence, because a bare 120x260 pixel
    crop of a person is not, on its own, "understandable" to someone
    scanning an event list on a phone.

Failure policy matches the rest of the AI pipeline (SPEC 43): every call is
wrapped, a failure degrades the feature rather than the event, and nothing
here can break ingestion.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import math
from dataclasses import dataclass
from typing import Protocol

import httpx

logger = logging.getLogger(__name__)

# Dimensionality of Azure AI Vision multimodal image vectors. Fixed by the
# service (model-version 2023-04-15); the local fallback matches it so the
# two are at least storage-compatible.
AZURE_IMAGE_EMBEDDING_DIMENSIONS = 1024

# The exact sentence the captioner must produce when the crop contains no
# visible person. It doubles as a false-positive signal: the local detector
# fires on shadows and foliage, and a caption model looking at the same crop
# is a far better judge of whether there is really someone there.
NO_PERSON_CAPTION = "No clear view of a person."

CAPTION_SYSTEM_PROMPT = (
    "You are labelling a still frame from a home security camera for the "
    "homeowner. Describe only what is visibly true about the person: rough "
    "build, clothing colours, and anything they are carrying. One short "
    "sentence, under 20 words. Never guess identity, name, age, ethnicity or "
    f"intent. If no person is clearly visible, reply exactly: {NO_PERSON_CAPTION}"
)


def caption_confirms_person(caption: str | None) -> bool:
    """Whether ``caption`` describes a genuinely visible person.

    A missing caption returns ``True``: when captioning is disabled or the
    call failed we have no second opinion, so we keep trusting the detector
    rather than silently dropping real sightings.
    """
    if caption is None:
        return True
    return caption.strip().rstrip(".").casefold() != NO_PERSON_CAPTION.rstrip(".").casefold()


def cosine_similarity(left: list[float], right: list[float]) -> float:
    """Cosine similarity of two equal-length vectors, clamped to -1..1."""
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return max(-1.0, min(1.0, dot / (left_norm * right_norm)))


def normalize(vector: list[float]) -> list[float]:
    """Return ``vector`` scaled to unit length (zero vector unchanged)."""
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return list(vector)
    return [value / norm for value in vector]


class ImageEmbedder(Protocol):
    name: str
    dimensions: int
    # Whether this embedder can actually generalize across pose/lighting,
    # i.e. whether automatic re-identification is meaningful. The local
    # fallback sets this False so callers can be honest in the API.
    semantic: bool

    async def embed_image(self, image: bytes) -> list[float]: ...


class ImageCaptioner(Protocol):
    name: str

    async def caption_image(self, image: bytes, content_type: str) -> str | None: ...


@dataclass
class LocalImageEmbedder:
    """Deterministic offline fallback. Explicitly *not* re-identification.

    This hashes image bytes into a stable unit vector. Two encodings of the
    exact same bytes match; two photos of the same person do not. It exists
    so the pipeline, the database schema, tests and the UI all work without
    any cloud dependency, and so a Foundry outage degrades to "every sighting
    is a new unknown person" rather than to a crash. ``semantic = False``
    tells the API to report that automatic recognition is unavailable instead
    of silently pretending it works.
    """

    dimensions: int = AZURE_IMAGE_EMBEDDING_DIMENSIONS
    name: str = "local-hash"
    semantic: bool = False

    async def embed_image(self, image: bytes) -> list[float]:
        values: list[float] = []
        counter = 0
        while len(values) < self.dimensions:
            digest = hashlib.sha256(image + counter.to_bytes(4, "big")).digest()
            values.extend((byte - 127.5) / 127.5 for byte in digest)
            counter += 1
        return normalize(values[: self.dimensions])


@dataclass
class AzureVisionImageEmbedder:
    """Azure AI Vision 4.0 multimodal image embeddings.

    Calls ``/computervision/retrieval:vectorizeImage`` on the Foundry
    account. This is the model that makes returning-visitor recognition
    actually work.
    """

    endpoint: str
    api_key: str
    api_version: str = "2024-02-01"
    model_version: str = "2023-04-15"
    timeout_seconds: float = 20.0
    dimensions: int = AZURE_IMAGE_EMBEDDING_DIMENSIONS
    name: str = "azure-vision-multimodal"
    semantic: bool = True

    @property
    def _url(self) -> str:
        base = self.endpoint.rstrip("/")
        return (
            f"{base}/computervision/retrieval:vectorizeImage"
            f"?api-version={self.api_version}&model-version={self.model_version}"
        )

    async def embed_image(self, image: bytes) -> list[float]:
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                self._url,
                content=image,
                headers={
                    "Ocp-Apim-Subscription-Key": self.api_key,
                    "Content-Type": "application/octet-stream",
                },
            )
        response.raise_for_status()
        vector = response.json().get("vector") or []
        if not vector:
            raise ValueError("Azure Vision returned an empty image vector")
        return normalize([float(value) for value in vector])


@dataclass
class AzureFoundryCaptioner:
    """Plain-language description of a person crop via a Foundry vision
    chat deployment."""

    endpoint: str
    api_key: str
    deployment: str
    api_version: str = "2024-10-21"
    timeout_seconds: float = 20.0
    name: str = "azure-foundry-vision"

    @property
    def _url(self) -> str:
        base = self.endpoint.rstrip("/")
        return f"{base}/openai/deployments/{self.deployment}/chat/completions?api-version={self.api_version}"

    async def caption_image(self, image: bytes, content_type: str = "image/jpeg") -> str | None:
        encoded = base64.b64encode(image).decode("ascii")
        payload = {
            "messages": [
                {"role": "system", "content": CAPTION_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe the person in this security camera crop."},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{content_type};base64,{encoded}"},
                        },
                    ],
                },
            ],
            "max_completion_tokens": 120,
        }
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                self._url,
                json=payload,
                headers={"api-key": self.api_key, "Content-Type": "application/json"},
            )
        response.raise_for_status()
        choices = response.json().get("choices") or []
        if not choices:
            return None
        content = (choices[0].get("message") or {}).get("content")
        if not content:
            return None
        return str(content).strip()[:500]


_embedder: ImageEmbedder | None = None
_captioner: ImageCaptioner | None = None


def _foundry_configured(settings) -> bool:
    return bool(settings.foundry_endpoint and settings.foundry_api_key)


def build_image_embedder(settings) -> ImageEmbedder:
    if not _foundry_configured(settings):
        logger.info("Foundry not configured; person recognition uses the local fallback embedder")
        return LocalImageEmbedder()
    return AzureVisionImageEmbedder(
        endpoint=settings.foundry_endpoint,
        api_key=settings.foundry_api_key,
        api_version=settings.foundry_embedding_api_version,
        model_version=settings.foundry_embedding_model_version,
        timeout_seconds=settings.foundry_timeout_seconds,
    )


def build_image_captioner(settings) -> ImageCaptioner | None:
    if not settings.person_caption_enabled or not _foundry_configured(settings):
        return None
    return AzureFoundryCaptioner(
        endpoint=settings.foundry_endpoint,
        api_key=settings.foundry_api_key,
        deployment=settings.foundry_vision_deployment,
        api_version=settings.foundry_vision_api_version,
        timeout_seconds=settings.foundry_timeout_seconds,
    )


def get_image_embedder() -> ImageEmbedder:
    global _embedder
    if _embedder is None:
        from ..config import settings

        _embedder = build_image_embedder(settings)
    return _embedder


def get_image_captioner() -> ImageCaptioner | None:
    global _captioner
    if _captioner is None:
        from ..config import settings

        _captioner = build_image_captioner(settings)
    return _captioner


def reset_vision_providers() -> None:
    """Test hook: drop the cached embedder/captioner singletons."""
    global _embedder, _captioner
    _embedder = None
    _captioner = None
