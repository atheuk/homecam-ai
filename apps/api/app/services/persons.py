"""Person identity and re-identification (returning-visitor recognition).

The user requirement is "over time the same person might return and I want
that to be recognized automatically". That is a *clustering* problem, not a
classification one: HomeCam is never told up front who exists. So:

1. Every person crop is embedded (see ``app/ai/vision.py``).
2. The vector is compared against the centroid of every known identity.
3. Above ``person_match_threshold`` it is the same person; the sighting is
   attached to that identity and (if trustworthy) refines their centroid.
4. Otherwise a brand-new, unnamed identity is created.

Naming is purely a human act layered on top: the clustering works before
anyone is named, and naming later retroactively labels every past sighting
of that identity, because they were already grouped.

Trust model (deliberate): only *human-confirmed* sightings are allowed to
add reference vectors to an identity. An automatic match rides on the
existing centroid but does not reinforce it. Without that rule a single
wrong auto-match would drag a centroid toward a second person and snowball
until the identity matches everyone.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..ai.vision import cosine_similarity, normalize
from ..config import settings
from ..models.db import Event, Person, PersonSighting

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MatchResult:
    person: Person
    similarity: float | None
    created: bool


def display_name(person: Person) -> str:
    """Human-facing label; unnamed identities still need to be referable."""
    if person.name:
        return person.name
    return f"Unknown person {person.id[-4:].upper()}"


def _recompute_centroid(samples: list[list[float]]) -> list[float]:
    """Mean of the reference vectors, re-normalized to unit length.

    Unit-norming matters: cosine similarity ignores magnitude, but keeping
    the centroid normalized keeps stored values comparable and bounded when
    samples are averaged repeatedly over months.
    """
    usable = [sample for sample in samples if sample]
    if not usable:
        return []
    width = len(usable[0])
    totals = [0.0] * width
    counted = 0
    for sample in usable:
        if len(sample) != width:
            continue
        for index, value in enumerate(sample):
            totals[index] += value
        counted += 1
    if not counted:
        return []
    return normalize([total / counted for total in totals])


async def list_persons(session: AsyncSession) -> list[Person]:
    result = await session.execute(select(Person).order_by(Person.last_seen_at.desc()))
    return list(result.scalars().all())


async def get_person(session: AsyncSession, person_id: str) -> Person | None:
    return await session.get(Person, person_id)


async def find_best_match(
    session: AsyncSession, embedding: list[float]
) -> tuple[Person | None, float]:
    """Nearest known identity to ``embedding`` and its similarity.

    Compared in Python rather than via pgvector on purpose: the candidate set
    is "people seen around one home", which is tens of rows, not millions.
    Keeping it here means identical behavior on SQLite (tests, local dev) and
    PostgreSQL (deployed), with no extension dependency. ``AIAnalysis`` already
    documents the same portability tradeoff.
    """
    if not embedding:
        return None, 0.0
    best: Person | None = None
    best_similarity = 0.0
    for person in await list_persons(session):
        if not person.centroid:
            continue
        similarity = cosine_similarity(embedding, person.centroid)
        if similarity > best_similarity:
            best, best_similarity = person, similarity
    return best, best_similarity


async def _create_person(session: AsyncSession, embedding: list[float], at: datetime) -> Person:
    person = Person(
        id="per-" + uuid.uuid4().hex[:16],
        name=None,
        centroid=list(embedding),
        embedding_dimensions=len(embedding),
        # The very first vector of a brand-new identity is its only evidence,
        # so it seeds the samples list even though no human has confirmed it.
        samples=[list(embedding)] if embedding else [],
        sighting_count=0,
        first_seen_at=at,
        last_seen_at=at,
        created_at=at,
        updated_at=at,
    )
    session.add(person)
    return person


def _add_reference_sample(person: Person, embedding: list[float]) -> None:
    """Teach an identity a new reference vector (human-confirmed only)."""
    if not embedding:
        return
    samples = [list(sample) for sample in (person.samples or [])]
    vector = list(embedding)
    # A brand-new identity is seeded with its first vector, and confirming
    # that same event would otherwise store it twice and double-weight it in
    # the centroid.
    if vector in samples:
        return
    samples.append(vector)
    if len(samples) > settings.person_max_samples:
        samples = samples[-settings.person_max_samples :]
    person.samples = samples
    person.centroid = _recompute_centroid(samples)
    person.embedding_dimensions = len(person.centroid)


async def record_sighting(
    session: AsyncSession,
    event: Event,
    embedding: list[float],
    at: datetime | None = None,
) -> MatchResult | None:
    """Attach ``event`` to an identity, matching or creating one.

    Returns ``None`` when recognition is disabled or nothing was embeddable,
    leaving the event exactly as it was.
    """
    if not settings.person_recognition_enabled or not embedding:
        return None
    at = at or datetime.now(timezone.utc)

    match, similarity = await find_best_match(session, embedding)
    if match is not None and similarity >= settings.person_match_threshold:
        person, created, recorded_similarity = match, False, similarity
    else:
        person = await _create_person(session, embedding, at)
        created, recorded_similarity = True, None

    person.sighting_count = (person.sighting_count or 0) + 1
    person.last_seen_at = at
    person.updated_at = at
    if person.cover_event_id is None:
        person.cover_event_id = event.id

    session.add(
        PersonSighting(
            id="sig-" + uuid.uuid4().hex[:16],
            person_id=person.id,
            event_id=event.id,
            camera_id=event.camera_id,
            similarity=recorded_similarity,
            assigned_by="new" if created else "auto",
            embedding=list(embedding),
            created_at=at,
        )
    )

    event.person_id = person.id
    event.person_confidence = recorded_similarity
    event.person_confirmed = False
    return MatchResult(person=person, similarity=recorded_similarity, created=created)


async def assign_person(
    session: AsyncSession,
    event: Event,
    person_id: str | None,
    name: str | None = None,
) -> Person | None:
    """Human assignment/correction of who is in an event.

    This is also the system's only learning signal: the corrected identity
    absorbs this event's embedding as a new reference vector, so the next
    visit matches automatically. Passing ``person_id=None`` with a ``name``
    creates a new identity from this event.
    """
    now = datetime.now(timezone.utc)
    embedding = await _embedding_for_event(session, event)

    if person_id:
        person = await session.get(Person, person_id)
        if person is None:
            return None
    else:
        person = await _create_person(session, embedding, now)
        person.sighting_count = 0

    if name is not None:
        person.name = name.strip()[:120] or None

    previous_id = event.person_id
    if previous_id and previous_id != person.id:
        await _detach_from_person(session, event, previous_id)

    if previous_id != person.id:
        person.sighting_count = (person.sighting_count or 0) + 1
    # Timestamps round-trip naive through SQLite, so both sides are coerced
    # to UTC-aware before comparison.
    seen_at = _aware(event.start_time, now)
    person.last_seen_at = max(_aware(person.last_seen_at, now), seen_at)
    person.first_seen_at = min(_aware(person.first_seen_at, now), seen_at)
    person.updated_at = now
    if person.cover_event_id is None:
        person.cover_event_id = event.id

    # The human said "this is them", so this vector is trustworthy evidence.
    _add_reference_sample(person, embedding)

    session.add(
        PersonSighting(
            id="sig-" + uuid.uuid4().hex[:16],
            person_id=person.id,
            event_id=event.id,
            camera_id=event.camera_id,
            similarity=None,
            assigned_by="manual",
            embedding=list(embedding),
            created_at=now,
        )
    )
    event.person_id = person.id
    event.person_confidence = None
    event.person_confirmed = True
    return person


async def rename_person(session: AsyncSession, person_id: str, name: str | None) -> Person | None:
    person = await session.get(Person, person_id)
    if person is None:
        return None
    person.name = (name or "").strip()[:120] or None
    person.updated_at = datetime.now(timezone.utc)
    return person


async def _detach_from_person(session: AsyncSession, event: Event, person_id: str) -> None:
    """Undo a previous (wrong) assignment, including anything it taught.

    Correcting a mislabel must actually *remove* the bad reference vector,
    otherwise the identity keeps drifting toward whoever was wrongly merged
    into it even after the visible label is fixed.
    """
    person = await session.get(Person, person_id)
    if person is None:
        return
    result = await session.execute(
        select(PersonSighting).where(
            PersonSighting.event_id == event.id, PersonSighting.person_id == person_id
        )
    )
    stale = list(result.scalars().all())
    stale_vectors = [list(sighting.embedding or []) for sighting in stale]
    for sighting in stale:
        await session.delete(sighting)

    remaining = [
        list(sample)
        for sample in (person.samples or [])
        if list(sample) not in stale_vectors
    ]
    person.samples = remaining
    person.centroid = _recompute_centroid(remaining)
    person.embedding_dimensions = len(person.centroid)
    person.sighting_count = max(0, (person.sighting_count or 0) - 1)
    if person.cover_event_id == event.id:
        person.cover_event_id = await _next_cover_event(session, person_id, exclude=event.id)
    person.updated_at = datetime.now(timezone.utc)


async def _next_cover_event(session: AsyncSession, person_id: str, exclude: str) -> str | None:
    result = await session.execute(
        select(PersonSighting.event_id)
        .where(PersonSighting.person_id == person_id, PersonSighting.event_id != exclude)
        .order_by(PersonSighting.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _embedding_for_event(session: AsyncSession, event: Event) -> list[float]:
    """Reuse the vector already computed for this event, if any."""
    result = await session.execute(
        select(PersonSighting.embedding)
        .where(PersonSighting.event_id == event.id)
        .order_by(PersonSighting.created_at.desc())
        .limit(1)
    )
    stored = result.scalar_one_or_none()
    return list(stored or [])


def _aware(value: datetime | None, fallback: datetime) -> datetime:
    if value is None:
        return fallback
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def sighting_counts(session: AsyncSession) -> dict[str, int]:
    result = await session.execute(
        select(PersonSighting.person_id, func.count(PersonSighting.id)).group_by(
            PersonSighting.person_id
        )
    )
    return {person_id: count for person_id, count in result.all()}


async def events_for_person(session: AsyncSession, person_id: str, limit: int = 50) -> list[Event]:
    result = await session.execute(
        select(Event)
        .where(Event.person_id == person_id)
        .order_by(Event.start_time.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


def to_dict(person: Person, sighting_count: int | None = None) -> dict:
    return {
        "id": person.id,
        "name": person.name,
        "display_name": display_name(person),
        "named": bool(person.name),
        "notes": person.notes,
        "sighting_count": sighting_count
        if sighting_count is not None
        else (person.sighting_count or 0),
        "reference_samples": len(person.samples or []),
        "cover_event_id": person.cover_event_id,
        "photo_url": f"/api/v1/persons/{person.id}/photo" if person.cover_event_id else None,
        "first_seen_at": person.first_seen_at.isoformat() if person.first_seen_at else None,
        "last_seen_at": person.last_seen_at.isoformat() if person.last_seen_at else None,
    }
