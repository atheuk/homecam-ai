"""Natural-language event search (Ring "Smart Video Search" / Nest "Ask Home"
class feature, built on what HomeCam already stores).

No new index and no new model: the AI pipeline already writes a text
embedding per analysed event (``AIAnalysis.embedding``), so search embeds
the *query* with the same provider and cosine-ranks the stored vectors.
Ranking runs in Python rather than in SQL so the identical code path works
on SQLite (host-native tests) and on the Compose/Azure PostgreSQL - see
docs/ai-pipeline.md for the pgvector follow-up.

The semantic score is blended with a deterministic keyword score over the
event's own summary/description/type/tags/zone. That blend matters: an
event that was never enriched has no embedding at all, and search must
still find it.

Every query passes through :mod:`app.ai.query_moderation` first, so no
amount of prompt phrasing can turn search into a face-recognition tool.
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..ai.provider import get_ai_provider
from ..ai.query_moderation import ModerationResult, moderate_query
from ..config import settings
from ..models.db import AIAnalysis, Event

logger = logging.getLogger(__name__)

_WORD_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    {
        "a", "an", "and", "any", "at", "be", "by", "for", "from", "in", "is", "it",
        "me", "my", "of", "on", "or", "show", "that", "the", "there", "to", "was",
        "were", "with",
    }
)


@dataclass(frozen=True)
class SearchHit:
    row: Event
    score: float
    semantic_score: float
    keyword_score: float


def tokenize(text: str) -> list[str]:
    return [word for word in _WORD_RE.findall((text or "").lower()) if word not in _STOPWORDS]


def cosine_similarity(left: list[float], right: list[float]) -> float:
    """Cosine similarity clamped to ``0..1``.

    Negative similarity is clamped rather than preserved: "the opposite of
    what you asked for" is not a search result, it is noise.
    """
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    norm_left = math.sqrt(sum(a * a for a in left))
    norm_right = math.sqrt(sum(b * b for b in right))
    if norm_left <= 0.0 or norm_right <= 0.0:
        return 0.0
    return max(0.0, min(1.0, dot / (norm_left * norm_right)))


def keyword_score(tokens: list[str], haystack: str) -> float:
    """Fraction of the query's words that appear in the event's own text."""
    if not tokens:
        return 0.0
    words = set(_WORD_RE.findall(haystack.lower()))
    hits = sum(1 for token in tokens if token in words or any(token in word for word in words))
    return hits / len(tokens)


def _haystack(row: Event, summary: str | None) -> str:
    metadata = row.event_metadata or {}
    vehicle = metadata.get("vehicle") or {}
    animal = metadata.get("animal") or {}
    parts = [
        row.type or "",
        row.zone or "",
        row.description or "",
        summary or "",
        " ".join(str(tag) for tag in (row.tags or [])),
        row.camera_id or "",
        " ".join(str(vehicle.get(key) or "") for key in ("make", "model", "colour", "body_type")),
        " ".join(str(animal.get(key) or "") for key in ("common_name", "scientific_name", "species", "breed")),
    ]
    return " ".join(parts)


async def _embed_query(text: str) -> list[float]:
    provider = get_ai_provider()
    if provider is None:
        return []
    try:
        return list(await provider.create_embedding(text))
    except Exception:  # noqa: BLE001 - search must degrade to keywords, not fail
        logger.warning("query embedding failed; falling back to keyword search", exc_info=True)
        return []


async def search_events(
    session: AsyncSession,
    query: str,
    *,
    camera_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int | None = None,
) -> tuple[ModerationResult, list[SearchHit]]:
    """Rank recent events against ``query``.

    Returns the moderation result alongside the hits so the caller can
    surface a refusal (no hits) or a notice (hits, plus an explanation of
    what was ignored) without re-running moderation.
    """
    moderation = moderate_query(query)
    if moderation.refused:
        return moderation, []

    limit = max(1, min(limit or settings.search_default_limit, settings.search_default_limit * 10))
    statement = select(Event, AIAnalysis).outerjoin(
        AIAnalysis, AIAnalysis.id == Event.ai_analysis_id
    )
    if camera_id:
        statement = statement.where(Event.camera_id == camera_id)
    if since is not None:
        statement = statement.where(Event.start_time >= since)
    if until is not None:
        statement = statement.where(Event.start_time <= until)
    statement = statement.order_by(Event.start_time.desc()).limit(settings.search_candidate_limit)
    rows = list((await session.execute(statement)).all())
    if not rows:
        return moderation, []

    tokens = tokenize(moderation.query)
    vector = await _embed_query(moderation.query)
    weight = settings.search_embedding_weight

    hits: list[SearchHit] = []
    for row, analysis in rows:
        summary = analysis.summary if analysis is not None else None
        semantic = 0.0
        if vector and analysis is not None:
            semantic = cosine_similarity(vector, list(analysis.embedding or []))
        keywords = keyword_score(tokens, _haystack(row, summary))
        # When nothing could be embedded on either side the blend would
        # silently halve a perfect keyword match, so fall back to the
        # keyword score alone rather than punishing un-enriched events.
        score = (weight * semantic + (1.0 - weight) * keywords) if semantic > 0.0 else keywords
        if score < settings.search_min_score:
            continue
        hits.append(SearchHit(row=row, score=score, semantic_score=semantic, keyword_score=keywords))

    hits.sort(key=lambda hit: (-hit.score, -hit.row.start_time.timestamp()))
    return moderation, hits[:limit]
