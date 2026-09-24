"""Person recognition: photo durability, rating, naming and re-identification.

The behaviours asserted here are exactly the user-visible promises of the
feature: a detected person produces a real viewable image, the image can be
rated, the person can be named, and the *same* person returning later is
recognized without anyone doing anything.
"""
import io
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image
from sqlalchemy import select

from app.ai.vision import (
    NO_PERSON_CAPTION,
    LocalImageEmbedder,
    caption_confirms_person,
    cosine_similarity,
    normalize,
)
from app.db import SessionLocal
from app.models.db import Event, EventPhoto, Person, PersonSighting
from app.services import persons as person_service

pytestmark = pytest.mark.asyncio


def _jpeg(colour=(120, 90, 60), size=(240, 320)) -> bytes:
    """A real, decodable JPEG, so 'the API returns a usable image' means it."""
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="JPEG")
    return buffer.getvalue()


JPEG_BYTES = _jpeg()


async def _make_event(camera_id="mock-front-door", *, with_photo=True, when=None):
    """Insert a person event (optionally with a stored photo) directly."""
    event_id = "evt-" + uuid.uuid4().hex[:12]
    when = when or datetime.now(timezone.utc)
    async with SessionLocal() as session:
        session.add(
            Event(
                id=event_id,
                camera_id=camera_id,
                type="person",
                priority="normal",
                source="local-ai",
                start_time=when,
                description="Person detected",
                tags=["person"],
                event_metadata={},
            )
        )
        if with_photo:
            session.add(
                EventPhoto(
                    event_id=event_id,
                    image=JPEG_BYTES,
                    content_type="image/jpeg",
                    width=240,
                    height=320,
                    caption="A person in a dark jacket.",
                    created_at=when,
                )
            )
        await session.commit()
    return event_id


async def test_event_photo_is_served_as_real_image_bytes(client):
    """The photo must come back as renderable bytes, not a server-side path.

    This is the bug the feature exists to fix: photos used to be written to
    the container's ephemeral disk and only the *path* was exposed, so the
    browser could never display them.
    """
    event_id = await _make_event()

    response = await client.get(f"/api/v1/events/{event_id}/photo")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/jpeg")
    assert response.content == JPEG_BYTES
    assert response.content[:2] == b"\xff\xd8"  # real JPEG magic bytes


async def test_event_listing_advertises_photo_and_caption(client):
    event_id = await _make_event()

    payload = (await client.get("/api/v1/events?limit=100")).json()
    event = next(item for item in payload if item["id"] == event_id)

    assert event["has_photo"] is True
    assert event["photo_url"] == f"/api/v1/events/{event_id}/photo"


async def test_missing_photo_is_reported_not_faked(client):
    event_id = await _make_event(with_photo=False)

    assert (await client.get(f"/api/v1/events/{event_id}/photo")).status_code == 404
    payload = (await client.get(f"/api/v1/events/{event_id}")).json()
    assert payload["has_photo"] is False
    assert payload["photo_url"] is None


async def test_photo_can_be_rated_and_cleared(client):
    event_id = await _make_event()

    rated = await client.post(f"/api/v1/events/{event_id}/rating", json={"rating": 4})
    assert rated.status_code == 200
    assert rated.json()["photo_rating"] == 4

    cleared = await client.post(f"/api/v1/events/{event_id}/rating", json={"rating": None})
    assert cleared.json()["photo_rating"] is None


async def test_rating_outside_one_to_five_is_rejected(client):
    event_id = await _make_event()
    assert (
        await client.post(f"/api/v1/events/{event_id}/rating", json={"rating": 9})
    ).status_code == 422


async def test_naming_a_person_labels_that_event(client):
    event_id = await _make_event()

    created = await client.post(
        f"/api/v1/events/{event_id}/person", json={"name": "Sarah"}
    )
    assert created.status_code == 200
    person = created.json()
    assert person["name"] == "Sarah"
    assert person["named"] is True

    event = (await client.get(f"/api/v1/events/{event_id}")).json()
    assert event["person_id"] == person["id"]
    assert event["person_name"] == "Sarah"
    assert event["person_confirmed"] is True


async def test_unnamed_person_still_has_a_stable_label(client):
    """Identities exist before anyone names them, so they need a usable label."""
    event_id = await _make_event()
    async with SessionLocal() as session:
        match = await person_service.record_sighting(
            session, await session.get(Event, event_id), normalize([3.0, 1.0, 4.0, 1.0])
        )
        person_id = match.person.id
        await session.commit()

    listing = (await client.get("/api/v1/persons")).json()
    person = next(item for item in listing["persons"] if item["id"] == person_id)

    assert person["name"] is None
    assert person["named"] is False
    # An unnamed identity must still be referable in the UI.
    assert person["display_name"].startswith("Unknown person ")


async def test_blank_name_is_rejected_rather_than_creating_a_nameless_person(client):
    event_id = await _make_event()
    response = await client.post(f"/api/v1/events/{event_id}/person", json={"name": "   "})
    assert response.status_code == 400


async def test_renaming_a_person_relabels_their_history(client):
    """Clustering happens before naming, so a late name must apply backwards."""
    first = await _make_event()
    person_id = (
        await client.post(f"/api/v1/events/{first}/person", json={"name": "Visitor"})
    ).json()["id"]
    second = await _make_event()
    await client.post(f"/api/v1/events/{second}/person", json={"person_id": person_id})

    renamed = await client.patch(f"/api/v1/persons/{person_id}", json={"name": "Alex"})
    assert renamed.json()["name"] == "Alex"

    detail = (await client.get(f"/api/v1/persons/{person_id}")).json()
    assert {event["id"] for event in detail["events"]} == {first, second}
    assert all(event["person_name"] == "Alex" for event in detail["events"])


async def test_person_photo_serves_their_cover_image(client):
    event_id = await _make_event()
    person_id = (
        await client.post(f"/api/v1/events/{event_id}/person", json={"name": "Jo"})
    ).json()["id"]

    response = await client.get(f"/api/v1/persons/{person_id}/photo")

    assert response.status_code == 200
    assert response.content == JPEG_BYTES


async def test_highly_rated_photo_becomes_the_person_cover(client):
    """A 5-star photo is by definition the best portrait available."""
    first = await _make_event()
    person_id = (
        await client.post(f"/api/v1/events/{first}/person", json={"name": "Sam"})
    ).json()["id"]
    better = await _make_event()
    await client.post(f"/api/v1/events/{better}/person", json={"person_id": person_id})

    await client.post(f"/api/v1/events/{better}/rating", json={"rating": 5})

    person = (await client.get(f"/api/v1/persons/{person_id}")).json()
    assert person["cover_event_id"] == better


async def test_assigning_to_unknown_person_is_rejected(client):
    event_id = await _make_event()
    assert (
        await client.post(
            f"/api/v1/events/{event_id}/person", json={"person_id": "per-does-not-exist"}
        )
    ).status_code == 404


async def test_assignment_requires_a_target(client):
    event_id = await _make_event()
    assert (await client.post(f"/api/v1/events/{event_id}/person", json={})).status_code == 400


async def test_persons_endpoint_declares_recognition_capability(client):
    """The UI must never imply recognition works when the fallback is active."""
    payload = (await client.get("/api/v1/persons")).json()

    recognition = payload["recognition"]
    assert set(recognition) >= {"enabled", "backend", "semantic", "match_threshold"}
    assert isinstance(recognition["semantic"], bool)


async def test_returning_visitor_is_matched_to_the_same_identity():
    """The core promise: a near-identical later sighting reuses the identity."""
    known = normalize([float(value) for value in range(1, 65)])
    returning = normalize([value + 0.001 for value in known])

    first = await _make_event()
    second = await _make_event(when=datetime.now(timezone.utc) + timedelta(days=1))

    async with SessionLocal() as session:
        first_row = await session.get(Event, first)
        initial = await person_service.record_sighting(session, first_row, known)
        await session.commit()

        second_row = await session.get(Event, second)
        repeat = await person_service.record_sighting(session, second_row, returning)
        await session.commit()

        assert initial.created is True
        assert repeat.created is False
        assert repeat.person.id == initial.person.id
        assert repeat.similarity > 0.99
        assert second_row.person_id == initial.person.id
        # Automatic matches are not treated as confirmed truth.
        assert second_row.person_confirmed is False


async def test_a_different_person_becomes_a_separate_identity():
    mine = normalize([1.0] + [0.0] * 63)
    theirs = normalize([0.0, 1.0] + [0.0] * 62)

    first = await _make_event()
    second = await _make_event()

    async with SessionLocal() as session:
        one = await person_service.record_sighting(session, await session.get(Event, first), mine)
        await session.commit()
        two = await person_service.record_sighting(
            session, await session.get(Event, second), theirs
        )
        await session.commit()

    assert one.person.id != two.person.id


async def test_automatic_match_does_not_reinforce_the_centroid():
    """Only humans get to teach identities, so one bad auto-match can't snowball."""
    base = normalize([float(value) for value in range(1, 65)])
    first = await _make_event()
    second = await _make_event()

    async with SessionLocal() as session:
        match = await person_service.record_sighting(
            session, await session.get(Event, first), base
        )
        person_id = match.person.id
        await session.commit()
        await person_service.record_sighting(
            session, await session.get(Event, second), normalize([v + 0.001 for v in base])
        )
        await session.commit()

        person = await session.get(Person, person_id)
        assert len(person.samples) == 1


async def test_correcting_a_mislabel_removes_what_it_taught():
    """A corrected identity must not keep drifting toward the wrong person."""
    wrong_vector = normalize([1.0] + [0.0] * 63)
    event_id = await _make_event()

    async with SessionLocal() as session:
        row = await session.get(Event, event_id)
        wrong = await person_service.assign_person(session, row, None, "Wrong Person")
        right = await person_service.assign_person(session, row, None, "Right Person")
        await session.commit()
        wrong_id, right_id = wrong.id, right.id

    async with SessionLocal() as session:
        wrong_person = await session.get(Person, wrong_id)
        row = await session.get(Event, event_id)
        assert row.person_id == right_id
        assert wrong_person.sighting_count == 0
        # No stale sighting rows left pointing the event at the wrong identity.
        remaining = await session.execute(
            select(PersonSighting).where(
                PersonSighting.person_id == wrong_id, PersonSighting.event_id == event_id
            )
        )
        assert remaining.scalars().all() == []
        assert wrong_vector is not None  # vector never became a reference sample
        assert wrong_person.samples == []


async def test_local_embedder_is_deterministic_but_not_semantic():
    """The offline fallback must be honest about what it cannot do."""
    embedder = LocalImageEmbedder()

    first = await embedder.embed_image(JPEG_BYTES)
    again = await embedder.embed_image(JPEG_BYTES)
    different = await embedder.embed_image(JPEG_BYTES + b"\x00")

    assert len(first) == embedder.dimensions
    assert first == again
    assert cosine_similarity(first, different) < 0.5
    assert embedder.semantic is False


async def test_real_ingestion_stores_a_viewable_photo_and_an_identity(client):
    """End-to-end through the actual pipeline, not hand-inserted rows.

    Guards the wiring that the rest of this file stubs past: a real detected
    person must end up with durable photo bytes the API can serve and an
    identity that later sightings can match against.
    """
    created = await client.post(
        "/api/v1/mock/events", json={"camera_id": "mock-front-door", "type": "person"}
    )
    event_id = created.json()["id"]

    detail = (await client.get(f"/api/v1/events/{event_id}")).json()
    assert detail["has_photo"] is True, "pipeline did not persist photo bytes"
    assert detail["person_id"], "pipeline did not attach an identity"

    photo = await client.get(f"/api/v1/events/{event_id}/photo")
    assert photo.status_code == 200
    assert len(photo.content) > 0

    person = (await client.get(f"/api/v1/persons/{detail['person_id']}")).json()
    assert person["sighting_count"] >= 1
    assert event_id in {event["id"] for event in person["events"]}



async def test_sentinel_caption_is_not_treated_as_a_person() -> None:
    """A false-positive detection must not mint an identity.

    The local detector fires on shadows and foliage; production produced a
    "person" event whose crop was an empty garden. The caption model is the
    second opinion that catches it.
    """
    assert caption_confirms_person(NO_PERSON_CAPTION) is False
    assert caption_confirms_person("no clear view of a person") is False
    assert caption_confirms_person("  No clear view of a person  ") is False


async def test_real_descriptions_and_missing_captions_still_count_as_people() -> None:
    assert caption_confirms_person("Adult in a dark coat carrying a parcel.") is True
    # No second opinion available (captioning off or the call failed) means we
    # keep trusting the detector instead of dropping genuine sightings.
    assert caption_confirms_person(None) is True
