"""Short event clips: validation, shared capture, budgets, edge fetch, playback."""
import asyncio
import struct
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select

from app.config import settings
from app.db import SessionLocal
from app.models.db import Event, EventClip, Incident, IncidentClip
from app.services import event_clips, events as event_service, mp4info
from app.services.incident_clips import CapturedClip


def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def _full(kind: bytes, payload: bytes, version: int = 0, flags: int = 0) -> bytes:
    return _box(kind, struct.pack(">I", (version << 24) | flags) + payload)


def make_fmp4(seconds: float = 2.0, timescale: int = 90000, fps: int = 10, codec: bytes = b"avc1") -> bytes:
    """Smallest fragmented MP4 that ``mp4info`` (and browsers' demuxers) accept."""
    tkhd = _full(b"tkhd", struct.pack(">IIIII", 0, 0, 1, 0, 0) + bytes(8) + bytes(8) + bytes(36)
                 + struct.pack(">II", 640 << 16, 360 << 16), flags=3)
    mdhd = _full(b"mdhd", struct.pack(">IIIIHH", 0, 0, timescale, 0, 0x55C4, 0))
    hdlr = _full(b"hdlr", struct.pack(">I", 0) + b"vide" + bytes(12) + b"video\x00")
    stsd = _full(b"stsd", struct.pack(">I", 1) + _box(codec, bytes(78)))
    stbl = _box(b"stbl", stsd)
    minf = _box(b"minf", stbl)
    mdia = _box(b"mdia", mdhd + hdlr + minf)
    trak = _box(b"trak", tkhd + mdia)
    trex = _full(b"trex", struct.pack(">IIIII", 1, 1, 0, 0, 0))
    moov = _box(b"moov", _full(b"mvhd", bytes(96)) + trak + _box(b"mvex", trex))
    samples = int(seconds * fps)
    per = timescale // fps
    tfhd = _full(b"tfhd", struct.pack(">II", 1, per), flags=0x08)
    trun = _full(b"trun", struct.pack(">I", samples) + struct.pack(">I", 4) * samples, flags=0x200)
    moof = _box(b"moof", _full(b"mfhd", struct.pack(">I", 1)) + _box(b"traf", tfhd + trun))
    mdat = _box(b"mdat", bytes(4 * samples))
    return _box(b"ftyp", b"isom\x00\x00\x02\x00isomiso6") + moov + moof + mdat


async def _headers(client) -> dict:
    login = await client.post("/api/v1/auth/login", json={
        "email": "pytest-default@homecam.test", "password": "pytest-password-123"
    })
    return {"Authorization": "Bearer " + login.json()["access_token"]}


async def _event(camera_id: str = "mock-front-door", kind: str = "person", **extra) -> str:
    event_id = f"clip-{uuid.uuid4().hex[:12]}"
    async with SessionLocal() as session:
        session.add(Event(
            id=event_id, camera_id=camera_id, type=kind, priority="normal", source="provider",
            start_time=datetime.now(timezone.utc), description="clip test", event_metadata={},
            tags=[], **extra,
        ))
        await session.commit()
    return event_id


async def _clip_state(event_id: str) -> dict:
    async with SessionLocal() as session:
        row = await session.get(Event, event_id)
        return event_clips.status(row)


@pytest.fixture
async def clean_clips():
    yield
    for task in list(event_clips._tasks):
        task.cancel()
    await event_clips.stop()
    event_clips._shared.clear()
    async with SessionLocal() as session:
        await session.execute(delete(EventClip))
        await session.execute(delete(Event).where(Event.id.like("clip-%")))
        await session.commit()


def test_mp4info_measures_real_duration_and_rejects_bad_media():
    info = mp4info.inspect(make_fmp4(3.0))
    assert info.codec == "avc1"
    assert info.duration_seconds == pytest.approx(3.0)
    assert info.fragmented is True
    assert (info.width, info.height) == (640, 360)
    video = make_fmp4(2.0)
    for bad in (b"", b"not a video at all", video[:40], video[:-20][:200], make_fmp4(codec=b"mp4a")):
        with pytest.raises(mp4info.InvalidClipError):
            mp4info.inspect(bad)


async def test_shared_capture_stores_one_clip_for_overlapping_events(client, anonymous_client, clean_clips):
    first, second = await _event(), await _event()
    video = CapturedClip(make_fmp4(16.0))
    video.pre_roll_seconds = 7.6
    clip_id = await event_clips.store("mock-front-door", video, "stream", None, {first: 0.0, second: 3.0})
    assert clip_id
    async with SessionLocal() as session:
        clips = list((await session.execute(select(EventClip))).scalars())
    assert len(clips) == 1 and sorted(clips[0].event_ids) == sorted([first, second])
    assert clips[0].duration_ms == 16000 and clips[0].pre_roll_ms == 7600

    state = await _clip_state(first)
    assert state["status"] == "ready"
    assert state["duration_seconds"] == 16.0 and state["pre_roll_seconds"] == 7.6
    assert state["codec"] == "avc1" and state["url"] == f"/api/v1/events/{first}/clip"
    assert (await _clip_state(second))["pre_roll_seconds"] == 10.6

    path = f"/api/v1/events/{first}/clip"
    assert (await anonymous_client.get(path)).status_code == 401
    headers = await _headers(client)
    response = await client.get(path, headers=headers)
    assert response.status_code == 200
    assert response.content == bytes(video)
    assert response.headers["content-type"] == "video/mp4"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    download = await client.get(path + "?download=true", headers=headers)
    assert "attachment" in download.headers["content-disposition"]
    listed = await client.get(f"/api/v1/events/{first}", headers=headers)
    if listed.status_code == 200:
        assert listed.json()["clip"]["status"] == "ready"


async def test_clip_endpoint_distinguishes_missing_pending_and_expired(client, clean_clips):
    headers = await _headers(client)
    event_id = await _event()
    assert (await client.get(f"/api/v1/events/{event_id}/clip", headers=headers)).status_code == 404
    assert (await client.get("/api/v1/events/nope/clip", headers=headers)).status_code == 404
    await event_clips.update_events([event_id], {"status": "pending", "source": "stream"}, only_pending=False)
    assert (await _clip_state(event_id))["status"] == "pending"
    assert (await client.get(f"/api/v1/events/{event_id}/clip", headers=headers)).status_code == 404

    clip_id = await event_clips.store("mock-front-door", make_fmp4(), "stream", None, {event_id: 0.0})
    async with SessionLocal() as session:
        clip = await session.get(EventClip, clip_id)
        clip.created_at = datetime.now(timezone.utc) - timedelta(days=settings.event_clip_retention_days + 1)
        await session.commit()
        assert await event_clips.expire(session) == 1
    state = await _clip_state(event_id)
    assert state["status"] == "expired" and state["url"] is None
    assert (await client.get(f"/api/v1/events/{event_id}/clip", headers=headers)).status_code in (404, 410)


async def test_expiry_keeps_clips_of_held_events(clean_clips):
    event_id = await _event(retention_hold=True)
    clip_id = await event_clips.store("mock-front-door", make_fmp4(), "stream", None, {event_id: 0.0})
    async with SessionLocal() as session:
        clip = await session.get(EventClip, clip_id)
        clip.created_at = datetime.now(timezone.utc) - timedelta(days=90)
        await session.commit()
        assert await event_clips.expire(session) == 0
        assert await session.get(EventClip, clip_id) is not None
    assert (await _clip_state(event_id))["status"] == "ready"


async def test_stale_pending_is_reported_as_interrupted(clean_clips):
    event_id = await _event()
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    await event_clips.update_events([event_id], {"status": "pending", "started_at": old}, only_pending=False)
    async with SessionLocal() as session:
        row = await session.get(Event, event_id)
        metadata = dict(row.event_metadata)
        metadata["clip"] = {**metadata["clip"], "started_at": old}
        row.event_metadata = metadata
        await session.commit()
    state = await _clip_state(event_id)
    assert state["status"] == "unavailable" and "interrupted" in state["reason"]


async def test_budgets_skip_without_evicting_held_incident_evidence(clean_clips, monkeypatch):
    now = datetime.now(timezone.utc)
    async with SessionLocal() as session:
        session.add(Incident(
            id="clip-held-incident", kind="intrusion", status="resolved", severity="high",
            camera_id="mock-front-door", mode_at_creation="away", event_ids=[], event_count=0,
            first_seen_at=now, last_seen_at=now, created_at=now, updated_at=now, summary="test",
            clip_hold=True, clip_status="ready",
        ))
        session.add(IncidentClip(incident_id="clip-held-incident", video=b"x" * 5000, size_bytes=5000, created_at=now))
        await session.commit()
    try:
        video = make_fmp4()
        # Aggregate cap counts the held incident clip.
        monkeypatch.setattr(settings, "incident_clip_storage_limit_bytes", 5000 + len(video) - 1)
        first = await _event()
        assert await event_clips.store("mock-front-door", video, "stream", None, {first: 0.0}) is None
        assert (await _clip_state(first))["status"] == "skipped"
        monkeypatch.setattr(settings, "incident_clip_storage_limit_bytes", 10**9)
        # Event sub-cap.
        monkeypatch.setattr(settings, "event_clip_storage_limit_bytes", len(video) - 1)
        second = await _event()
        assert await event_clips.store("mock-front-door", video, "stream", None, {second: 0.0}) is None
        monkeypatch.setattr(settings, "event_clip_storage_limit_bytes", 10**9)
        # Daily count, raced by concurrent admissions: exactly one wins.
        monkeypatch.setattr(settings, "event_clip_daily_limit", 1)
        racers = [await _event() for _ in range(4)]
        results = await asyncio.gather(*[
            event_clips.store("mock-front-door", video, "stream", None, {event_id: 0.0}) for event_id in racers
        ])
        assert sum(1 for result in results if result) == 1
        statuses = sorted([(await _clip_state(event_id))["status"] for event_id in racers])
        assert statuses == ["ready", "skipped", "skipped", "skipped"]
        async with SessionLocal() as session:
            assert await session.get(IncidentClip, "clip-held-incident") is not None
            assert len(list((await session.execute(select(EventClip))).scalars())) == 1
    finally:
        async with SessionLocal() as session:
            await session.execute(delete(IncidentClip).where(IncidentClip.incident_id == "clip-held-incident"))
            await session.execute(delete(Incident).where(Incident.id == "clip-held-incident"))
            await session.commit()


async def test_invalid_or_oversized_media_is_never_stored(clean_clips, monkeypatch):
    event_id = await _event()
    assert await event_clips.store("mock-front-door", b"\x00\x00\x00\x10ftypisom", "stream", None, {event_id: 0.0}) is None
    assert (await _clip_state(event_id))["status"] == "unavailable"
    video = make_fmp4()
    monkeypatch.setattr(settings, "incident_clip_max_bytes", len(video) - 1)
    other = await _event()
    assert await event_clips.store("mock-front-door", video, "stream", None, {other: 0.0}) is None
    assert "size cap" in (await _clip_state(other))["reason"]


class _EdgeProvider:
    def __init__(self, clips, video):
        self.clips = clips
        self.video = video
        self.fetches = 0

    def has_camera(self, camera_id):
        return camera_id == "eufy-T8210"

    async def get_capabilities(self, camera_id):
        return {"eventClips": "SUPPORTED"}

    async def list_event_clips(self, camera_id):
        return self.clips

    async def get_event_clip(self, camera_id, clip_id):
        self.fetches += 1
        return self.video


async def test_edge_clip_is_fetched_matched_and_deduplicated(clean_clips, monkeypatch):
    video = make_fmp4(12.0)
    started = datetime.now(timezone.utc)
    provider = _EdgeProvider([
        {"id": "old", "started_at": (started - timedelta(minutes=5)).isoformat(), "complete": True},
        {"id": "doorbell-1", "started_at": (started + timedelta(seconds=2)).isoformat(),
         "pre_roll_seconds": 0, "complete": True},
    ], video)

    async def find(camera_id):
        return provider if provider.has_camera(camera_id) else None

    monkeypatch.setattr("app.services.provider_registry.find_provider_for_camera", find)
    monkeypatch.setattr(event_clips, "_EDGE_POLL_SECONDS", 0.01)
    first = await _event("eufy-T8210", "doorbell")
    second = await _event("eufy-T8210", "person")
    for event_id in (first, second):
        async with SessionLocal() as session:
            row = await session.get(Event, event_id)
            leftover, changed = await event_clips.attach(session, row, None)
            assert leftover is None and changed
            assert event_clips.status(row)["status"] == "pending"
        await asyncio.gather(*list(event_clips._tasks))
    for event_id in (first, second):
        state = await _clip_state(event_id)
        assert state["status"] == "ready" and state["source"] == "edge"
        assert state["pre_roll_seconds"] == 0.0 and state["duration_seconds"] == 12.0
    async with SessionLocal() as session:
        clips = list((await session.execute(select(EventClip))).scalars())
    assert len(clips) == 1 and clips[0].source_ref == "eufy-T8210:doorbell-1"
    assert sorted(clips[0].event_ids) == sorted([first, second])


async def test_edge_unsupported_and_no_match_are_reported_honestly(clean_clips, monkeypatch):
    provider = _EdgeProvider(None, b"")

    async def find(camera_id):
        return provider

    monkeypatch.setattr("app.services.provider_registry.find_provider_for_camera", find)
    monkeypatch.setattr(event_clips, "_EDGE_POLL_SECONDS", 0.01)
    monkeypatch.setattr(settings, "event_clip_edge_wait_seconds", 0.05)
    event_id = await _event("eufy-T8210", "doorbell")
    async with SessionLocal() as session:
        await event_clips.attach(session, await session.get(Event, event_id), None)
    await asyncio.gather(*list(event_clips._tasks))
    assert (await _clip_state(event_id))["status"] == "unsupported"

    provider.clips = [{"id": "x", "started_at": "2001-01-01T00:00:00+00:00", "complete": True}]
    other = await _event("eufy-T8210", "doorbell")
    async with SessionLocal() as session:
        await event_clips.attach(session, await session.get(Event, other), None)
    await asyncio.gather(*list(event_clips._tasks))
    state = await _clip_state(other)
    assert state["status"] == "unavailable" and "did not record" in state["reason"]
    assert provider.fetches == 0


@pytest.mark.parametrize(("capability", "expected", "reason"), [
    ("UNSUPPORTED", "unsupported", "cannot record"),
    ("UNAVAILABLE", "unavailable", "recorder is not available"),
])
async def test_recorder_capability_is_reported_without_waiting(clean_clips, monkeypatch, capability, expected, reason):
    provider = _EdgeProvider([], b"")

    async def caps(camera_id):
        return {"eventClips": capability}

    async def find(camera_id):
        return provider

    provider.get_capabilities = caps
    monkeypatch.setattr("app.services.provider_registry.find_provider_for_camera", find)
    event_id = await _event("eufy-T8210", "doorbell")
    async with SessionLocal() as session:
        row = await session.get(Event, event_id)
        await event_clips.attach(session, row, None)
        state = event_clips.status(row)
    assert state["status"] == expected and reason in state["reason"]
    assert not event_clips._tasks and provider.fetches == 0


async def test_camera_without_buffer_or_recorder_and_unwanted_types(clean_clips):
    event_id = await _event("mock-front-door", "person")
    async with SessionLocal() as session:
        row = await session.get(Event, event_id)
        await event_clips.attach(session, row, None)
        state = event_clips.status(row)
    assert state["status"] == "unavailable" and "No buffered live video" in state["reason"]
    assert not event_clips._tasks
    quiet = await _event("mock-front-door", "camera_offline")
    async with SessionLocal() as session:
        row = await session.get(Event, quiet)
        capture, changed = await event_clips.attach(session, row, None)
        assert (capture, changed) == (None, False)
        assert event_clips.status(row)["status"] == "none"


async def test_second_event_joins_running_stream_capture(clean_clips, monkeypatch):
    release = asyncio.Event()
    video = CapturedClip(make_fmp4(16.0))
    video.pre_roll_seconds = 8.0

    async def capture():
        await release.wait()
        return video

    first, second = await _event(), await _event(kind="animal")
    task = asyncio.create_task(capture())
    async with SessionLocal() as session:
        leftover, _ = await event_clips.attach(session, await session.get(Event, first), task)
        assert leftover is None
    other_task = asyncio.create_task(capture())
    async with SessionLocal() as session:
        leftover, _ = await event_clips.attach(session, await session.get(Event, second), other_task)
        # The caller must discard the redundant capture it offered.
        assert leftover is other_task
    other_task.cancel()
    release.set()
    await asyncio.gather(*list(event_clips._tasks))
    first_state, second_state = await _clip_state(first), await _clip_state(second)
    assert first_state["status"] == second_state["status"] == "ready"
    assert first_state["pre_roll_seconds"] == 8.0
    assert second_state["pre_roll_seconds"] >= 8.0


async def test_incident_linked_event_serves_the_incident_clip(client, clean_clips):
    headers = await _headers(client)
    now = datetime.now(timezone.utc)
    event_id = await _event()
    async with SessionLocal() as session:
        session.add(Incident(
            id="clip-linked", kind="intrusion", status="open", severity="high",
            camera_id="mock-front-door", mode_at_creation="away", event_ids=[event_id], event_count=1,
            first_seen_at=now, last_seen_at=now, created_at=now, updated_at=now, summary="test",
            clip_status="ready",
        ))
        session.add(IncidentClip(incident_id="clip-linked", video=make_fmp4(), size_bytes=1, created_at=now))
        await session.commit()
    try:
        await event_clips.link_incident(event_id, "clip-linked")
        await event_clips.update_events([event_id], {"status": "ready", **event_clips.clip_facts(make_fmp4())})
        response = await client.get(f"/api/v1/events/{event_id}/clip", headers=headers)
        assert response.status_code == 200 and response.content == make_fmp4()
        state = await _clip_state(event_id)
        assert state["source"] == "incident" and state["incident_id"] == "clip-linked"
    finally:
        async with SessionLocal() as session:
            await session.execute(delete(IncidentClip).where(IncidentClip.incident_id == "clip-linked"))
            await session.execute(delete(Incident).where(Incident.id == "clip-linked"))
            await session.commit()


def test_event_dict_exposes_clip_status_without_bytes():
    row = Event(
        id="clip-dict", camera_id="mock-front-door", type="person", priority="normal", source="provider",
        start_time=datetime.now(timezone.utc), description="x", event_metadata={}, tags=[],
    )
    assert event_service.to_dict(row)["clip"] == {"status": "none", "url": None}
