"""Incident clip capture, playback, human holds and retention."""
import asyncio
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.config import settings
from app.db import SessionLocal
from app.models.db import Incident, IncidentClip
from app.services import incident_clips, retention, stream_frames
from app.services.stream_frames import ClipSegment, FrameSample


async def test_clip_capture_playback_hold_and_retention(client, anonymous_client, monkeypatch):
    login = await client.post("/api/v1/auth/login", json={
        "email": "pytest-default@homecam.test", "password": "pytest-password-123"
    })
    headers = {"Authorization": "Bearer " + login.json()["access_token"]}
    now = datetime.now(timezone.utc)
    camera_id = "mock-front-door"
    incident_id = "clip-test"
    init = b"\x00\x00\x00\x10ftypisom"
    segment = b"\x00\x00\x00\x10moofvideo-data"
    sample_time = time.monotonic()
    sample = ClipSegment(init, segment, sample_time, 1)
    hub = stream_frames.stream_hub
    original = hub._readers.get(camera_id)
    hub._readers[camera_id] = SimpleNamespace(
        unsupported=False,
        latest=FrameSample((b"frame",), sample_time, 1, "segment-1"),
        clip_segments=[sample],
    )
    try:
        async with SessionLocal() as session:
            session.add(Incident(
                id=incident_id, kind="intrusion", status="resolved", severity="high",
                camera_id=camera_id, mode_at_creation="away", event_ids=[], event_count=0,
                first_seen_at=now - timedelta(days=40), last_seen_at=now - timedelta(days=40),
                created_at=now, updated_at=now, summary="test",
            ))
            await session.commit()
        assert incident_clips.seed(camera_id) == (sample,)
        async with SessionLocal() as session:
            row = await session.get(Incident, incident_id)
            row.clip_status = "pending"
            await session.commit()
        monkeypatch.setattr(settings, "incident_clip_post_seconds", 1)
        capture = incident_clips.capture(camera_id, (sample,), sample_time - 1)
        await incident_clips._finish(incident_id, capture)

        path = f"/api/v1/security/incidents/{incident_id}/clip"
        assert (await anonymous_client.get(path)).status_code == 401
        response = await client.get(path, headers=headers)
        assert response.status_code == 200
        assert response.content == init + segment
        assert response.headers["content-type"] == "video/mp4"
        assert response.headers["cache-control"] == "private, no-store"
        assert "attachment" in (await client.get(path + "?download=true", headers=headers)).headers["content-disposition"]
        held = await client.put(path + "/hold", json={"hold": True}, headers=headers)
        assert held.status_code == 200
        assert held.json()["clip_hold"] is True
        assert held.json()["clip"]["status"] == "ready"
        assert (await anonymous_client.put(path + "/hold", json={"hold": False})).status_code == 401
        async with SessionLocal() as session:
            report = await retention.run(session, dry_run=False, now=now)
            assert report.counts["media"] >= 0
            assert await session.get(IncidentClip, incident_id) is not None
        unheld = await client.put(path + "/hold", json={"hold": False}, headers=headers)
        assert unheld.status_code == 200
        async with SessionLocal() as session:
            await retention.run(session, dry_run=False, now=now)
            assert await session.get(IncidentClip, incident_id) is None
        assert (await client.get(path, headers=headers)).status_code == 404
    finally:
        for task in list(incident_clips._tasks):
            task.cancel()
        await incident_clips.stop()
        async with SessionLocal() as session:
            row = await session.get(Incident, incident_id)
            if row is not None:
                await session.delete(row)
                await session.commit()
        if original is None:
            hub._readers.pop(camera_id, None)
        else:
            hub._readers[camera_id] = original


async def test_post_roll_is_captured_while_incident_enrichment_is_slow(client, monkeypatch):
    camera_id = "mock-garden"
    incident_id = "clip-slow-analysis"
    now = datetime.now(timezone.utc)
    triggered_at = time.monotonic()
    init = b"\x00\x00\x00\x10ftypisom"
    first = ClipSegment(init, b"\x00\x00\x00\x10moof-first", triggered_at, 1)
    reader = SimpleNamespace(
        unsupported=False,
        latest=FrameSample((b"frame",), triggered_at, 1, "segment-1"),
        clip_segments=[first],
    )
    hub = stream_frames.stream_hub
    previous = hub._readers.get(camera_id)
    hub._readers[camera_id] = reader
    monkeypatch.setattr(settings, "incident_clip_post_seconds", 0.2)
    try:
        async with SessionLocal() as session:
            session.add(Incident(
                id=incident_id, kind="intrusion", status="open", severity="high",
                camera_id=camera_id, mode_at_creation="away", event_ids=[], event_count=0,
                first_seen_at=now, last_seen_at=now, created_at=now, updated_at=now, summary="test",
            ))
            await session.commit()
            incident = await session.get(Incident, incident_id)
        task = incident_clips.capture(camera_id, (first,), triggered_at)
        await asyncio.sleep(0.05)
        captured_at = time.monotonic()
        second = ClipSegment(init, b"\x00\x00\x00\x10moof-second", captured_at, 2)
        reader.clip_segments.append(second)
        reader.latest = FrameSample((b"frame",), captured_at, 2, "segment-2")
        await asyncio.sleep(0.25)
        await incident_clips.start(incident, task)
        await asyncio.gather(*list(incident_clips._tasks))
        async with SessionLocal() as session:
            clip = await session.get(IncidentClip, incident_id)
            assert clip is not None
            assert clip.video == init + first.data + second.data
            await session.delete(clip)
            await session.delete(await session.get(Incident, incident_id))
            await session.commit()
    finally:
        await incident_clips.stop()
        if previous is None:
            hub._readers.pop(camera_id, None)
        else:
            hub._readers[camera_id] = previous
