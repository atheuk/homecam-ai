"""Behaviour scores cannot be manufactured from clothing or demographics."""
from datetime import datetime, timedelta, timezone

import pytest

from app.ai.detector import BoundingBox, Detection
from app.config import settings
from app.db import SessionLocal, init_db
from app.models.db import Event, Incident, Person
from app.services import incidents, scene_state, suspicious
from sqlalchemy import delete


def test_hoodie_alone_never_alerts():
    for night in (False, True):
        verdict = suspicious.score({}, night=night, mode="away")
        assert verdict["score"] == 0
        assert verdict["level"] is None
        assert verdict["reasons"] == []


def test_behavioral_evidence_explains_score_without_protected_attributes():
    verdict = suspicious.score(
        {"vehicle_seconds": 130, "circling": True,
         "behaviours": ["hands_at_vehicle", "gender", "ethnicity", "age"]},
        night=True, mode="away",
    )
    assert verdict["level"] == "suspicious"
    assert not any("hood" in reason for reason in verdict["reasons"])
    assert not any(word in " ".join(verdict["reasons"]).casefold()
                   for word in ("gender", "ethnicity", "race", "age", "identity"))


def test_returning_text_uses_similarity_not_identity():
    verdict = suspicious.score({"return_visits": 3, "evidence_event_ids": ["one", "two", "three"]})
    assert verdict["level"] == "elevated"
    assert verdict["reasons"][0] == "a person with a similar appearance was seen here 3 times in the last 24 hours"
    assert "same person" not in verdict["reasons"][0]
    assert verdict["evidence_event_ids"] == ["one", "two", "three"]


@pytest.mark.asyncio
async def test_observation_is_per_spatial_track_and_deduped():
    await init_db()
    scene = scene_state.CameraScene(camera_id="front")
    person = Detection("person", 0.95, BoundingBox(.15, .2, .35, .8))
    car = scene_state.Track(
        id="car", camera_id="front", label="car", zone="driveway", state="stable",
        box=BoundingBox(.3, .3, .8, .8), observation_count=6,
        first_seen_at=100, last_seen_at=100, anchored_at=100,
    )
    scene.tracks["car"] = car
    async with SessionLocal() as session:
        assert await suspicious.observe(session, scene, [person], [], 100) == []
        car.state = "tracking"
        car.data["interacted"] = True
        assert await suspicious.observe(session, scene, [person], [], 130) == []
        # A gap over 20s resets a visit; continuous observations cross 45s.
        for at in (140, 150, 160, 170, 180):
            events = await suspicious.observe(session, scene, [person], [], at)
        assert len(events) == 1
        assert events[0].event_type == "suspicious_activity"
        assert await suspicious.observe(session, scene, [person], [], 185) == []


@pytest.mark.asyncio
async def test_trusted_person_lingering_beside_car_produces_no_suspicious_event():
    await init_db()
    now = datetime.now(timezone.utc)
    box = BoundingBox(.15, .2, .35, .8)
    scene = scene_state.CameraScene(camera_id="front-trusted")
    scene.tracks["car"] = scene_state.Track(
        id="car", camera_id="front-trusted", label="car", zone="driveway", state="stable",
        box=BoundingBox(.3, .3, .8, .8), observation_count=6,
        first_seen_at=now.timestamp(), last_seen_at=now.timestamp(), anchored_at=now.timestamp(),
    )
    async with SessionLocal() as session:
        person = Person(id="test-trusted-lingering", name="Household", trust="trusted",
                        centroid=[1.0], samples=[[1.0]], embedding_dimensions=1, sighting_count=1,
                        first_seen_at=now, last_seen_at=now, created_at=now, updated_at=now)
        event = Event(id="test-trusted-sighting", camera_id="front-trusted", type="person",
                      priority="normal", source="local-ai", start_time=now, description="person seen",
                      person_id=person.id, person_confidence=settings.person_match_threshold,
                      event_metadata={"best_photo": {"detection": {
                          "label": "person", "bbox": box.as_dict(),
                      }}})
        session.add_all([person, event])
        await session.flush()
        detection = Detection("person", .95, box)
        for step in range(0, 91, 10):
            assert await suspicious.observe(session, scene, [detection], [],
                                            now.timestamp() + step) == []
        assert all(record.data.get("person_id") == person.id for record in scene.zones.values())
        assert await suspicious.is_trusted_event(session, event)
        await session.rollback()


@pytest.mark.asyncio
async def test_trusted_and_named_people_excluded_from_returning_visits():
    await init_db()
    at = datetime.now(timezone.utc)
    async with SessionLocal() as session:
        for trust, name in (("trusted", None), ("unknown", "Alex")):
            person_id = f"test-suspicious-{trust}-{name}"
            person = Person(id=person_id, name=name, trust=trust, centroid=[1.0], samples=[[1.0]],
                            embedding_dimensions=1, sighting_count=3, first_seen_at=at,
                            last_seen_at=at, created_at=at, updated_at=at)
            session.add(person)
            for idx in range(settings.suspicious_return_visits):
                event = Event(id=f"{person_id}-{idx}", camera_id="front", type="person",
                              priority="normal", source="local-ai", start_time=at - timedelta(hours=idx),
                              description="person seen", person_id=person_id,
                              person_confidence=settings.person_match_threshold)
                session.add(event)
            await session.flush()
            assert await suspicious.returning_visits(session, event, at, False) == {}
        await session.rollback()


@pytest.mark.asyncio
async def test_returning_appearance_counts_distinct_visits_not_frames():
    await init_db()
    at = datetime.now(timezone.utc)
    person_id = "test-possibly-returning-appearance"
    async with SessionLocal() as session:
        session.add(Person(
            id=person_id, name=None, trust="unknown", centroid=[1.0], samples=[[1.0]],
            embedding_dimensions=1, sighting_count=4, first_seen_at=at,
            last_seen_at=at, created_at=at, updated_at=at,
        ))
        events = []
        for idx, seconds in enumerate((0, 60, 400, 800)):
            event = Event(
                id=f"test-return-visit-{idx}", camera_id="front", type="person",
                priority="normal", source="local-ai", start_time=at + timedelta(seconds=seconds),
                description="person seen", person_id=person_id,
                person_confidence=settings.person_match_threshold,
            )
            session.add(event)
            events.append(event)
        await session.flush()
        early = await suspicious.returning_visits(
            session, events[1], events[1].start_time, False,
        )
        assert early == {}
        third = await suspicious.returning_visits(
            session, events[-1], events[-1].start_time, False,
        )
        assert third["return_visits"] == 3
        assert third["evidence_event_ids"] == [events[i].id for i in (0, 2, 3)]
        await session.rollback()


@pytest.mark.asyncio
async def test_suspicious_incident_requires_armed_mode():
    await init_db()
    now = datetime.now(timezone.utc)
    async with SessionLocal() as session:
        event = Event(
            id="test-suspicious-incident", camera_id="front", type="suspicious_activity",
            priority="high", source="local-ai", start_time=now, description="activity",
            event_metadata={"notification_priority": "high", "suspicious": {
                "score": 5.5, "level": "suspicious",
                "reasons": ["lingered 130s beside a parked vehicle"], "evidence_event_ids": [],
            }},
        )
        session.add(event)
        await session.commit()
        assert await incidents.route_event(session, event, mode="home") is None
        assert await incidents.route_event(session, event, mode="disarmed") is None
        incident = await incidents.route_event(session, event, mode="away")
        assert incident is not None and incident.kind == "suspicious_activity"
        assert incident.evidence["score"] == 5.5
        await session.execute(delete(Incident).where(Incident.id == incident.id))
        await session.delete(event)
        await session.commit()
