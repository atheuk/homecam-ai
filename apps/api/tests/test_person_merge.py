"""Identity consolidation: folding duplicate clusters into one person.

Why this exists: clustering is deliberately cautious, so one real person can
accumulate several unnamed identities over weeks of poor-light sightings.
Naming is the moment to repair that, because the user has just supplied
ground truth and each cluster now has a multi-sample centroid that is far
less noisy than the single sighting vector that originally failed to match.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models.db import Event, Person, PersonSighting
from app.services import persons as person_service

pytestmark = pytest.mark.asyncio


def _vector(seed: float, length: int = 32) -> list[float]:
    """A deterministic unit-ish vector; near-identical seeds are similar."""
    raw = [seed + (index * 0.001) for index in range(length)]
    magnitude = sum(value * value for value in raw) ** 0.5
    return [value / magnitude for value in raw]


async def _make_person(
    *,
    name=None,
    samples=None,
    sighting_count=1,
    first_seen=None,
    last_seen=None,
    trust="unknown",
):
    person_id = "per-" + uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc)
    samples = samples or [_vector(0.5)]
    centroid = person_service._recompute_centroid(samples)
    async with SessionLocal() as session:
        session.add(
            Person(
                id=person_id,
                name=name,
                trust=trust,
                centroid=centroid,
                embedding_dimensions=len(centroid),
                samples=samples,
                sighting_count=sighting_count,
                first_seen_at=first_seen or now,
                last_seen_at=last_seen or now,
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()
    return person_id


async def _make_sighting(person_id, *, camera_id="mock-front-door"):
    event_id = "evt-" + uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc)
    async with SessionLocal() as session:
        session.add(
            Event(
                id=event_id,
                camera_id=camera_id,
                type="person",
                priority="normal",
                source="local-ai",
                start_time=now,
                description="Person detected",
                tags=["person"],
                event_metadata={},
                person_id=person_id,
            )
        )
        session.add(
            PersonSighting(
                id="sig-" + uuid.uuid4().hex[:12],
                person_id=person_id,
                event_id=event_id,
                camera_id=camera_id,
                similarity=0.97,
                assigned_by="auto",
                embedding=_vector(0.5),
                created_at=now,
            )
        )
        await session.commit()
    return event_id


async def test_trust_defaults_to_unknown_and_is_never_inferred(client):
    person_id = await _make_person()
    async with SessionLocal() as session:
        person = await person_service.get_person(session, person_id)
        assert person_service.trust_of(person) == "unknown"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("trusted", "trusted"),
        ("WATCH", "watch"),
        ("  trusted ", "trusted"),
        ("nonsense", "unknown"),
        (None, "unknown"),
    ],
)
async def test_trust_values_are_normalized(raw, expected):
    assert person_service.normalize_trust(raw) == expected


async def test_naming_absorbs_a_near_identical_unnamed_identity(client):
    shared = _vector(0.5)
    keeper = await _make_person(samples=[shared])
    duplicate = await _make_person(samples=[shared])
    duplicate_event = await _make_sighting(duplicate)

    async with SessionLocal() as session:
        person = await person_service.get_person(session, keeper)
        person.name = "Sarah"
        merged = await person_service.consolidate_identity(session, person)
        await session.commit()

    assert duplicate in merged
    async with SessionLocal() as session:
        assert await person_service.get_person(session, duplicate) is None
        # The duplicate's history survives; only the identity row is gone.
        event = await session.get(Event, duplicate_event)
        assert event.person_id == keeper
        rows = await session.execute(
            select(PersonSighting).where(PersonSighting.person_id == keeper)
        )
        assert len(rows.scalars().all()) == 1


async def test_a_clearly_different_person_is_not_absorbed(client):
    keeper = await _make_person(samples=[_vector(0.5)])
    stranger = await _make_person(samples=[_vector(-0.9)])
    async with SessionLocal() as session:
        person = await person_service.get_person(session, keeper)
        person.name = "Sarah"
        merged = await person_service.consolidate_identity(session, person)
        await session.commit()
    assert stranger not in merged
    async with SessionLocal() as session:
        assert await person_service.get_person(session, stranger) is not None


async def test_an_already_named_identity_is_never_auto_absorbed(client):
    """Folding "Dad" into "Sarah" would destroy an explicit user decision."""
    shared = _vector(0.5)
    keeper = await _make_person(samples=[shared])
    named_twin = await _make_person(name="Dad", samples=[shared])
    async with SessionLocal() as session:
        person = await person_service.get_person(session, keeper)
        person.name = "Sarah"
        merged = await person_service.consolidate_identity(session, person)
        await session.commit()
    assert named_twin not in merged
    async with SessionLocal() as session:
        assert await person_service.get_person(session, named_twin) is not None


async def test_unnamed_identities_are_left_alone(client):
    """Consolidation only runs on the evidence a name provides."""
    shared = _vector(0.5)
    keeper = await _make_person(samples=[shared])
    duplicate = await _make_person(samples=[shared])
    async with SessionLocal() as session:
        person = await person_service.get_person(session, keeper)
        merged = await person_service.consolidate_identity(session, person)
        await session.commit()
    assert merged == []
    async with SessionLocal() as session:
        assert await person_service.get_person(session, duplicate) is not None


async def test_merge_unions_samples_and_sums_counts(client):
    older = datetime.now(timezone.utc) - timedelta(days=9)
    newer = datetime.now(timezone.utc)
    keeper = await _make_person(
        samples=[_vector(0.5)], sighting_count=3, first_seen=newer, last_seen=newer
    )
    source = await _make_person(
        samples=[_vector(0.5001)], sighting_count=4, first_seen=older, last_seen=older
    )
    async with SessionLocal() as session:
        target = await person_service.get_person(session, keeper)
        other = await person_service.get_person(session, source)
        await person_service.merge_person(session, target, other)
        await session.commit()

    async with SessionLocal() as session:
        person = await person_service.get_person(session, keeper)
        assert person.sighting_count == 7
        assert len(person.samples) == 2
        # The merged identity must cover the whole observed history.
        assert person.first_seen_at.replace(tzinfo=timezone.utc) == pytest.approx(
            older, abs=timedelta(seconds=1)
        )


async def test_merge_endpoint_requires_two_distinct_identities(client):
    person_id = await _make_person()
    response = await client.post(
        f"/api/v1/persons/{person_id}/merge", json={"source_id": person_id}
    )
    assert response.status_code == 400


async def test_merge_endpoint_folds_identities(client):
    shared = _vector(0.5)
    keeper = await _make_person(name="Sarah", samples=[shared])
    source = await _make_person(samples=[shared])
    response = await client.post(
        f"/api/v1/persons/{keeper}/merge", json={"source_id": source}
    )
    assert response.status_code == 200
    assert response.json()["merged_person_ids"] == [source]
    missing = await client.get(f"/api/v1/persons/{source}")
    assert missing.status_code == 404


async def test_duplicates_endpoint_suggests_without_applying(client):
    shared = _vector(0.5)
    keeper = await _make_person(name="Sarah", samples=[shared])
    duplicate = await _make_person(samples=[shared])
    response = await client.get(f"/api/v1/persons/{keeper}/duplicates")
    assert response.status_code == 200
    ids = [item["id"] for item in response.json()["candidates"]]
    assert duplicate in ids
    # Suggesting must not have silently merged anything.
    still_there = await client.get(f"/api/v1/persons/{duplicate}")
    assert still_there.status_code == 200


async def test_trust_can_be_set_through_the_api(client):
    person_id = await _make_person()
    response = await client.patch(
        f"/api/v1/persons/{person_id}", json={"trust": "trusted"}
    )
    assert response.status_code == 200
    assert response.json()["trust"] == "trusted"


async def test_api_rejects_an_unsupported_trust_value(client):
    person_id = await _make_person()
    response = await client.patch(
        f"/api/v1/persons/{person_id}", json={"trust": "dangerous"}
    )
    assert response.status_code == 422


async def test_naming_through_the_api_reports_merged_identities(client):
    shared = _vector(0.5)
    keeper = await _make_person(samples=[shared])
    duplicate = await _make_person(samples=[shared])
    response = await client.patch(
        f"/api/v1/persons/{keeper}", json={"name": "Sarah"}
    )
    assert response.status_code == 200
    assert duplicate in response.json()["merged_person_ids"]
