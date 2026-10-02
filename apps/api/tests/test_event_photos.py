import asyncio
import io
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from PIL import Image
from sqlalchemy import delete

from app.config import settings
from app.ai.detector import BoundingBox, Detection
from app.db import SessionLocal
from app.models.db import Event, EventPhoto
from app.services import ai_pipeline, event_photos, events, ingestion, provider_registry


def jpeg():
    output = io.BytesIO()
    Image.new("RGB", (160, 120), "blue").save(output, "JPEG")
    return output.getvalue()


def event():
    return {
        "id": "photo-" + uuid.uuid4().hex, "camera_id": "mock-front-door",
        "type": "motion", "priority": "normal", "source": "provider",
        "start_time": datetime.now(timezone.utc).isoformat(), "description": "Camera motion",
    }


@pytest.fixture(autouse=True)
async def cleanup(client):
    ingestion._last_snapshot_at.clear()
    yield
    await event_photos.stop()
    ingestion._last_snapshot_at.clear()
    async with SessionLocal() as session:
        await session.execute(delete(Event).where(Event.id.like("photo-%")))
        await session.commit()


@pytest.mark.parametrize("ai_enabled,event_type", [(True, "motion"), (False, "motion"), (True, "battery_low")])
async def test_every_camera_event_keeps_unverified_frame(client, monkeypatch, ai_enabled, event_type):
    monkeypatch.setattr(settings, "ai_analysis_enabled", ai_enabled)
    monkeypatch.setattr(ai_pipeline, "get_detector", lambda: SimpleNamespace(detect=lambda *_: []))
    monkeypatch.setattr(ai_pipeline, "_embed_photo", lambda *_: pytest.fail("fallback must not be embedded"))
    payload = {**event(), "type": event_type}
    async with SessionLocal() as session:
        row = await events.create_and_broadcast_event(session, payload, trigger_frame=jpeg())
        assert await session.get(EventPhoto, row.id) is not None
        result = events.to_dict(row)
        assert result["photo_capture_status"] == "captured"
        assert result["photo_fallback"] is True
        assert result["photo_verified"] is False
        assert result["photo_boxes"] == result["full_photo_boxes"] == []
        assert row.person_id is None
    denied = await client.post(f"/api/v1/events/{payload['id']}/person", json={"name": "Someone"})
    assert denied.status_code == 409


async def test_failed_snapshot_is_backfilled_and_broadcast(client, monkeypatch):
    calls = []
    class Provider:
        async def get_snapshot(self, camera_id):
            calls.append(camera_id)
            if len(calls) == 1:
                raise OSError("camera busy")
            return jpeg()
    async def find(_):
        return Provider()
    monkeypatch.setattr(provider_registry, "find_provider_for_camera", find)
    monkeypatch.setattr(event_photos, "find_provider_for_camera", find)
    monkeypatch.setattr(ai_pipeline, "get_detector", lambda: SimpleNamespace(detect=lambda *_: []))
    queue = events.event_bus.subscribe()
    try:
        async with SessionLocal() as session:
            row = await events.create_and_broadcast_event(session, event())
            event_id = row.id
        created = await queue.get()
        assert created["photo_capture_status"] == "pending"
        assert not created["has_photo"]
        updated = await asyncio.wait_for(queue.get(), 3)
        assert updated["_sse_event"] == "event.updated"
        assert updated["has_photo"] and updated["photo_url"].endswith("/photo")
        assert updated["photo_verified"] is False
        async with SessionLocal() as session:
            assert await session.get(EventPhoto, event_id) is not None
        assert len(calls) == 2
    finally:
        events.event_bus.unsubscribe(queue)


async def test_eufy_event_image_is_used_without_snapshot_or_live(client, monkeypatch):
    monkeypatch.setattr(settings, "ai_analysis_enabled", False)
    class Provider:
        async def get_event_snapshot(self, _):
            return jpeg()
        async def get_snapshot(self, _):
            pytest.fail("cached provider picture must win")
        async def get_live_stream(self, _):
            pytest.fail("no live stream for photo capture")
    async def find(_):
        return Provider()
    monkeypatch.setattr(event_photos, "find_provider_for_camera", find)
    async with SessionLocal() as session:
        row = await events.create_and_broadcast_event(session, event())
        await asyncio.wait_for(event_photos._tasks[row.id], 3)
        await session.refresh(row)
        assert events.to_dict(row)["photo_fallback"]
        assert row.event_metadata["photo_capture"]["source"] == "provider_event"


async def test_stream_cache_precedes_provider_calls(client, monkeypatch):
    monkeypatch.setattr(event_photos.stream_hub, "latest", lambda _: SimpleNamespace(frame=jpeg()))
    async def forbidden(_):
        pytest.fail("cached frame needs no provider session")
    monkeypatch.setattr(event_photos, "find_provider_for_camera", forbidden)
    image, source = await event_photos._acquire("mock-front-door")
    assert image == jpeg() and source == "stream"


async def test_exhausted_capture_reports_failure(client, monkeypatch):
    async def unavailable(_):
        raise TimeoutError
    monkeypatch.setattr(event_photos, "_acquire", unavailable)
    async with SessionLocal() as session:
        row = await events.persist_event(session, event())
        await event_photos.backfill(row.id, row.camera_id)
        await session.refresh(row)
        assert events.to_dict(row)["photo_capture_status"] == "failed"
        assert events.to_dict(row)["photo_capture_reason"] == "Camera did not return an image"


async def test_deleted_event_is_not_resurrected(client, monkeypatch):
    async def acquire(_):
        return jpeg(), "snapshot"
    monkeypatch.setattr(event_photos, "_acquire", acquire)
    await event_photos.backfill("photo-deleted", "mock-front-door")
    async with SessionLocal() as session:
        assert await session.get(EventPhoto, "photo-deleted") is None


def test_corrupt_or_oversized_frames_are_not_stored():
    assert event_photos.fallback_photo([b"not an image", b"x" * (event_photos.MAX_IMAGE_BYTES + 1)]) is None


def test_restart_never_leaves_an_old_pending_card_spinning():
    row = Event(**{key: value for key, value in event().items() if key != "start_time"},
                start_time=datetime.now(timezone.utc) - timedelta(minutes=10),
                event_metadata={"photo_capture": {"status": "pending"}})
    assert event_photos.capture_state(row) == {"status": "failed", "reason": "Photo capture was interrupted"}


async def test_delayed_provider_event_starts_capture_now(client, monkeypatch):
    async def acquire(_):
        return jpeg(), "provider_event"
    monkeypatch.setattr(event_photos, "_acquire", acquire)
    payload = {**event(), "start_time": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()}
    async with SessionLocal() as session:
        row = await events.persist_event(session, payload)
        assert event_photos.capture_state(row)["status"] == "pending"
        assert event_photos.schedule(row)
        await asyncio.wait_for(event_photos._tasks[row.id], 3)
        await session.refresh(row)
        assert await session.get(EventPhoto, row.id) is not None


async def test_cache_is_available_even_when_provider_slots_are_busy(client, monkeypatch):
    monkeypatch.setattr(event_photos.stream_hub, "latest", lambda _: SimpleNamespace(frame=jpeg()))
    slots = asyncio.Semaphore(0)
    monkeypatch.setattr(event_photos, "_slots", slots)
    image, source = await asyncio.wait_for(event_photos._bounded_acquire("mock-front-door"), 1)
    assert image == jpeg() and source == "stream"


async def test_late_image_prefers_subject_without_identity_inference(client, monkeypatch):
    detector = SimpleNamespace(detect=lambda *_: [Detection("person", 0.95, BoundingBox(.2, .1, .7, .9))])
    monkeypatch.setattr(event_photos, "get_detector", lambda: detector)
    async def acquire(_):
        return jpeg(), "provider_event"
    monkeypatch.setattr(event_photos, "_acquire", acquire)
    monkeypatch.setattr(ai_pipeline, "_embed_photo", lambda *_: pytest.fail("late image must not train identities"))
    async with SessionLocal() as session:
        row = await events.persist_event(session, {**event(), "type": "person"})
        await event_photos.backfill(row.id, row.camera_id)
        await session.refresh(row)
        result = events.to_dict(row)
        assert result["photo_capture_status"] == "captured"
        assert result["photo_fallback"] is False
        assert result["photo_verified"] is None
        assert result["photo_boxes"][0]["label"] == "person"
        assert row.person_id is None
