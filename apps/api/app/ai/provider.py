"""AI provider abstraction (SPEC section 14).

HomeCam Core talks only to :class:`AIProvider`. It never imports an AI
vendor SDK, so swapping in an Azure OpenAI / OpenAI / local implementation
later is a drop-in addition under this package.

:class:`MockAIProvider` is the default: fully deterministic, no network
calls, and grounded exclusively in the detections/zone facts it is given.
"""
from __future__ import annotations

import hashlib
import logging
import math
from dataclasses import dataclass, field
from typing import Protocol

from .detector import ANIMAL_CLASSES, VEHICLE_CLASSES, Detection
from .schemas import AIAnswer, ImageAnalysis, assert_grounded

logger = logging.getLogger(__name__)


@dataclass
class AnalysisContext:
    """Normalized facts available to an AI provider. No provider types."""

    camera_id: str
    camera_name: str
    event_type: str
    detections: list[Detection] = field(default_factory=list)
    zone: str | None = None
    tags: list[str] = field(default_factory=list)

    @property
    def observed_labels(self) -> set[str]:
        return {detection.label for detection in self.detections}


class AIProvider(Protocol):
    name: str
    model: str

    async def analyze_image(self, image: bytes, context: AnalysisContext) -> ImageAnalysis: ...

    async def create_embedding(self, text: str) -> list[float]: ...

    async def answer_question(self, question: str, events: list[dict]) -> AIAnswer: ...


def _category(context: AnalysisContext) -> str:
    labels = context.observed_labels
    if "mailbox" in context.tags:
        return "mailbox"
    if "package" in labels:
        return "delivery"
    if context.event_type == "doorbell" or "person" in labels and context.zone in {"entry", "front-door"}:
        return "visitor"
    if labels & VEHICLE_CLASSES:
        return "vehicle"
    if labels & ANIMAL_CLASSES:
        return "animal"
    if "person" in labels:
        return "person"
    if labels:
        return "motion"
    return "unknown"


def _importance(context: AnalysisContext, category: str) -> str:
    if context.event_type == "intrusion":
        return "critical"
    if category in {"visitor", "delivery", "mailbox"}:
        return "high"
    if category in {"person", "vehicle"}:
        return "normal"
    return "low"


def _actions(context: AnalysisContext) -> list[str]:
    """Describe only what the detections + zones actually support."""
    actions: list[str] = []
    zone = context.zone
    for detection in context.detections:
        where = f" in the {zone}" if zone else ""
        if detection.label == "person":
            if "driveway-access" in context.tags:
                actions.append(f"person present in the {zone}")
            elif "mailbox" in context.tags:
                actions.append(f"person reaching the {zone}")
            else:
                actions.append(f"person detected{where}")
        elif detection.label in VEHICLE_CLASSES:
            actions.append(f"{detection.label} {'parked' if 'parked' in context.tags else 'moving'}{where}")
        elif detection.label in ANIMAL_CLASSES:
            actions.append(f"{detection.label} passing{where}")
        elif detection.label == "package":
            actions.append(f"package visible{where}")
    return actions


def _hedged(confidence: float, sentence: str) -> str:
    """SPEC 15: use uncertainty-aware language for uncertain classifications."""
    if confidence >= 0.85:
        return sentence
    if confidence >= 0.6:
        return f"Likely: {sentence}"
    return f"Possibly (low confidence): {sentence}"


class MockAIProvider:
    """Deterministic, grounded, offline AI provider (SPEC section 14/40)."""

    name = "mock"
    model = "homecam-mock-analyzer-v1"

    def __init__(self, embedding_dimensions: int = 384) -> None:
        self.embedding_dimensions = embedding_dimensions

    async def analyze_image(self, image: bytes, context: AnalysisContext) -> ImageAnalysis:
        labels = sorted(context.observed_labels)
        category = _category(context)
        confidence = (
            round(min(0.97, sum(d.confidence for d in context.detections) / len(context.detections)), 4)
            if context.detections
            else 0.2
        )
        where = f" in the {context.zone}" if context.zone else ""
        if labels:
            subject = ", ".join(labels)
            sentence = f"{subject} detected{where} on {context.camera_name}."
            if "parked" in context.tags:
                sentence = f"A vehicle appears parked{where or ' on the driveway'} at {context.camera_name}."
            elif "mailbox" in context.tags:
                sentence = f"Activity at the mailbox zone on {context.camera_name}."
        else:
            sentence = f"Motion reported by {context.camera_name} without a confirmed object."
        analysis = ImageAnalysis(
            summary=_hedged(confidence, sentence),
            objects=labels,
            actions=_actions(context),
            event_category=category,
            importance=_importance(context, category),
            confidence=confidence,
        )
        return assert_grounded(analysis, context.observed_labels)

    async def create_embedding(self, text: str) -> list[float]:
        """Deterministic unit-norm pseudo-embedding of configurable width."""
        dimensions = max(1, self.embedding_dimensions)
        values: list[float] = []
        counter = 0
        while len(values) < dimensions:
            digest = hashlib.sha256(f"{text}|{counter}".encode()).digest()
            values.extend((byte - 127.5) / 127.5 for byte in digest)
            counter += 1
        values = values[:dimensions]
        norm = math.sqrt(sum(value * value for value in values)) or 1.0
        return [round(value / norm, 6) for value in values]

    async def answer_question(self, question: str, events: list[dict]) -> AIAnswer:
        if not events:
            return AIAnswer(
                answer="No recorded events match that question.", event_ids=[], confidence=0.3
            )
        event_ids = [str(event["id"]) for event in events]
        lines = [
            f"{event.get('start_time')}: {event.get('description') or event.get('type')}"
            for event in events[:5]
        ]
        answer = f"Based on {len(events)} recorded event(s):\n" + "\n".join(lines)
        return AIAnswer(answer=answer, event_ids=event_ids, confidence=0.7)


_active_provider: AIProvider | None = None


def build_ai_provider(name: str, embedding_dimensions: int) -> AIProvider:
    normalized = (name or "mock").strip().lower()
    if normalized != "mock":
        # Real cloud providers are a later phase; degrade loudly, not silently.
        logger.warning("AI provider '%s' is not implemented yet; using MockAIProvider", name)
    return MockAIProvider(embedding_dimensions=embedding_dimensions)


def get_ai_provider() -> AIProvider:
    global _active_provider
    if _active_provider is None:
        from ..config import settings

        _active_provider = build_ai_provider(settings.ai_provider, settings.embedding_dimensions)
    return _active_provider


def reset_ai_provider() -> None:
    global _active_provider
    _active_provider = None
