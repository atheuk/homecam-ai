"""Bounded, event-triggered post-roll clips on the Eufy edge adapter.

No real bridge, device or go2rtc: the bridge is a fake client and go2rtc an
httpx MockTransport.
"""
from __future__ import annotations

import asyncio
import struct

import httpx
import pytest
from fastapi.testclient import TestClient

import app as adapter
from eufy_ws import EufyWsClient

SERIAL = "T8210P1234567890"
TOKEN = "test-token"


def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def _full(kind: bytes, payload: bytes, flags: int = 0) -> bytes:
    return _box(kind, struct.pack(">I", flags) + payload)


def fmp4(seconds: float = 2.0, timescale: int = 90000, fps: int = 10) -> bytes:
    mdhd = _full(b"mdhd", struct.pack(">IIIIHH", 0, 0, timescale, 0, 0, 0))
    moov = _box(b"moov", _box(b"trak", _box(b"mdia", mdhd)))
    samples = int(seconds * fps)
    tfhd = _full(b"tfhd", struct.pack(">II", 1, timescale // fps), flags=0x08)
    trun = _full(b"trun", struct.pack(">I", samples) + struct.pack(">I", 4) * samples, flags=0x200)
    moof = _box(b"moof", _box(b"traf", tfhd + trun))
    return _box(b"ftyp", b"isom\x00\x00\x02\x00") + moov + moof + _box(b"mdat", bytes(4 * samples))


class FakeClient:
    def __init__(self):
        self.started: list[str] = []

    async def ensure_connected(self):
        return None

    def has_device(self, serial):
        return serial == SERIAL

    def devices(self):
        return [{"id": SERIAL, "name": "Front Doorbell", "type": "doorbell", "model": "T8210", "online": True,
                 "battery_level": 80, "capabilities": {"liveStream": True, "doorbellEvents": True}}]

    async def start_livestream(self, serial):
        self.started.append(serial)

    async def close(self):
        return None


@pytest.fixture
def fake(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(adapter, "client", client)
    monkeypatch.setattr(adapter.settings, "edge_token", TOKEN)
    monkeypatch.setattr(adapter.settings, "event_clips_enabled", True)
    recorder = adapter.ClipRecorder(enabled=True, seconds=15, cooldown=120, daily_limit=3)
    monkeypatch.setattr(adapter, "recorder", recorder)

    async def ensure(serial):
        return f"eufy-{serial}"

    monkeypatch.setattr(adapter, "_ensure_go2rtc_stream", ensure)
    return client


def auth():
    return {"Authorization": "Bearer " + TOKEN}


def test_trigger_events_and_rising_properties_fire_listeners(monkeypatch):
    client = EufyWsClient("ws://bridge")
    fired: list = []
    client.trigger_listeners.append(lambda serial, trigger: fired.append((serial, trigger)))
    client.trigger_listeners.append(lambda serial, trigger: 1 / 0)  # a broken listener is contained
    client._handle_event({"event": "rings", "serialNumber": SERIAL, "state": True})
    client._handle_event({"event": "motion detected", "serialNumber": SERIAL, "state": False})
    client._handle_event({"event": "person detected", "serialNumber": SERIAL, "state": True})
    client._handle_event({"event": "property changed", "serialNumber": SERIAL, "name": "motionDetected", "value": True})
    client._handle_event({"event": "property changed", "serialNumber": SERIAL, "name": "motionDetected", "value": True})
    client._handle_event({"event": "property changed", "serialNumber": SERIAL, "name": "battery", "value": 50})
    assert fired == [(SERIAL, "doorbell"), (SERIAL, "person"), (SERIAL, "motion")]


def test_recorder_records_post_roll_only_and_is_bounded(fake, monkeypatch):
    video = fmp4(14.0)
    fetches: list = []

    async def fetch(name, seconds):
        fetches.append((name, seconds))
        return video

    monkeypatch.setattr(adapter, "_fetch_mp4", fetch)

    async def scenario():
        recorder = adapter.recorder
        first = recorder.trigger(SERIAL, "doorbell")
        assert first is not None
        assert recorder.trigger(SERIAL, "motion") is None  # already recording
        await asyncio.gather(*recorder._tasks)
        assert recorder.trigger(SERIAL, "motion") is None  # cooldown
        (summary,) = recorder.list(SERIAL)
        assert summary["complete"] is True and summary["pre_roll_seconds"] == 0.0
        assert summary["duration_seconds"] == 14.0 and summary["size_bytes"] == len(video)
        assert recorder.get(SERIAL, first.id).data == video
        recorder._last.clear()
        recorder._today = recorder.daily_limit
        assert recorder.trigger(SERIAL, "motion") is None  # daily cap

    asyncio.run(scenario())
    assert fetches == [(f"eufy-{SERIAL}", 15)]
    assert fake.started == [SERIAL]


def test_failed_recording_is_reported_without_bytes_or_secrets(fake, monkeypatch):
    async def fetch(name, seconds):
        raise RuntimeError("go2rtc refused http://x/api?token=supersecret")

    monkeypatch.setattr(adapter, "_fetch_mp4", fetch)

    async def scenario():
        clip = adapter.recorder.trigger(SERIAL, "doorbell")
        await asyncio.gather(*adapter.recorder._tasks)
        return clip

    clip = asyncio.run(scenario())
    assert clip.complete is False and clip.data == b""
    assert "supersecret" not in clip.failed


def test_disabled_recorder_never_wakes_the_device(fake):
    recorder = adapter.ClipRecorder(enabled=False, seconds=15, cooldown=120, daily_limit=3)
    assert recorder.trigger(SERIAL, "doorbell") is None
    assert fake.started == []


def test_clip_endpoints_require_the_token_and_serve_mp4(fake):
    video = fmp4()
    clip = adapter.EdgeClip("abc", SERIAL, "doorbell", adapter.datetime.now(adapter.timezone.utc),
                            adapter.time.monotonic(), video, 2.0, True)
    adapter.recorder.clips[SERIAL] = adapter.deque([clip], maxlen=10)
    with TestClient(adapter.app) as http:
        assert http.get(f"/devices/{SERIAL}/clips").status_code == 401
        assert http.get(f"/devices/{SERIAL}/clips/abc").status_code == 401
        devices = http.get("/devices", headers=auth()).json()["devices"]
        assert devices[0]["capabilities"]["eventClips"] is True
        listed = http.get(f"/devices/{SERIAL}/clips", headers=auth()).json()["clips"]
        assert [c["id"] for c in listed] == ["abc"]
        response = http.get(f"/devices/{SERIAL}/clips/abc", headers=auth())
        assert response.status_code == 200 and response.content == video
        assert response.headers["content-type"] == "video/mp4"
        assert response.headers["cache-control"] == "no-store"
        assert http.get(f"/devices/{SERIAL}/clips/nope", headers=auth()).status_code == 404
        assert http.get("/devices/unknown/clips", headers=auth()).status_code == 404
        adapter.settings.event_clips_enabled = False
        try:
            assert http.get(f"/devices/{SERIAL}/clips", headers=auth()).status_code == 404
            assert http.get("/devices", headers=auth()).json()["devices"][0]["capabilities"]["eventClips"] is False
        finally:
            adapter.settings.event_clips_enabled = True


def test_expired_clips_are_dropped(fake):
    clip = adapter.EdgeClip("old", SERIAL, "motion", adapter.datetime.now(adapter.timezone.utc),
                            adapter.time.monotonic() - adapter.CLIP_TTL_SECONDS - 1, fmp4(), 2.0, True)
    adapter.recorder.clips[SERIAL] = adapter.deque([clip], maxlen=10)
    assert adapter.recorder.list(SERIAL) == []


def test_fetch_mp4_asks_go2rtc_for_a_bounded_recording_and_trims_partial_boxes(monkeypatch):
    video = fmp4(3.0)
    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=video + b"\x00\x00\x10\x00moof-partial")

    real = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(adapter.httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(adapter.settings, "go2rtc_url", "http://127.0.0.1:21984")
    data = asyncio.run(adapter._fetch_mp4("eufy-X", 15))
    assert data == video
    assert adapter._mp4_duration(data) == 3.0
    assert seen[0].url.path == "/api/stream.mp4"
    assert dict(seen[0].url.params) == {"src": "eufy-X", "duration": "15"}


def test_mp4_helpers_are_safe_on_junk():
    assert adapter._mp4_complete_prefix(b"\x00\x00") == b""
    assert adapter._mp4_duration(b"garbage-garbage") is None
