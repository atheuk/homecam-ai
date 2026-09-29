"""Stream-based frame source and stationary-object suppression (incident fix).

Production incident: the edge connector's 4K ``snapshot.cgi`` was refused
69-82% of the time by a session-limited Dahua NVR, so ingestion saw about
one frame per camera every ~100s and people crossing in 5-10s were never
sampled, while a parked car re-emitted a vehicle event every ~3 minutes.
"""
from __future__ import annotations

import logging
import time
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import delete

from app.ai.detector import BoundingBox, Detection, mock_detector
from app.config import settings
from app.db import SessionLocal
from app.models.db import Activity, AIAnalysis, Event
from app.providers.mock import mock_provider
from app.services import ingestion, stream_frames
from app.services.stream_frames import (
    FrameSample,
    StreamFrameHub,
    StreamFrameReader,
    _QuietStreamPolling,
    latest_segment,
    stream_hub,
    variant_playlist_url,
)

from test_subject_selection import CAR, CAR_RGB, PERSON, PERSON_RGB, _scene

CAMERA = "mock-front-door"

# Shapes captured from the production MediaMTX relay (LL-HLS, fMP4).
MASTER = """#EXTM3U
#EXT-X-VERSION:9
#EXT-X-INDEPENDENT-SEGMENTS

#EXT-X-STREAM-INF:BANDWIDTH=1068558,CODECS="avc1.64001e",RESOLUTION=704x576,FRAME-RATE=20.000
video1_stream.m3u8
"""


def _media(first: int = 7, last: int = 9, gaps: int = 2) -> str:
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:10",
        "#EXT-X-TARGETDURATION:2",
        "#EXT-X-PART-INF:PART-TARGET=0.52000",
        '#EXT-X-MAP:URI="abc_video1_init.mp4"',
    ]
    for _ in range(gaps):
        lines += ["#EXT-X-GAP", "#EXTINF:2.11000,", "gap.mp4"]
    for number in range(first, last + 1):
        lines += [
            "#EXTINF:2.00000,",
            f"abc_video1_seg{number}.mp4",
            f'#EXT-X-PART:DURATION=0.48000,URI="abc_video1_part{number * 4}.mp4",INDEPENDENT=YES',
        ]
    lines.append(f'#EXT-X-PRELOAD-HINT:TYPE=PART,URI="abc_video1_part{last * 4 + 1}.mp4"')
    return "\n".join(lines) + "\n"


BASE = "http://100.64.0.1:8888/dahua-2/index.m3u8"


def test_master_playlist_resolves_to_its_variant():
    assert variant_playlist_url(MASTER, BASE) == "http://100.64.0.1:8888/dahua-2/video1_stream.m3u8"
    assert variant_playlist_url(_media(), BASE) is None


def test_newest_complete_segment_skips_parts_hints_and_gaps():
    init, segment = latest_segment(_media(), BASE)
    assert init == "http://100.64.0.1:8888/dahua-2/abc_video1_init.mp4"
    assert segment == "http://100.64.0.1:8888/dahua-2/abc_video1_seg9.mp4"
    # A freshly started muxer that only has gap padding has nothing to decode.
    assert latest_segment(_media(first=1, last=0, gaps=3), BASE) == (
        "http://100.64.0.1:8888/dahua-2/abc_video1_init.mp4",
        None,
    )


# --- reader ------------------------------------------------------------------


class _Relay:
    """In-memory MediaMTX: serves master, media playlist, init and segments."""

    def __init__(self) -> None:
        self.last = 9
        self.requests: list[str] = []
        self.fail = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append(path)
        if self.fail:
            return httpx.Response(503)
        if path.endswith("index.m3u8"):
            return httpx.Response(200, text=MASTER)
        if path.endswith("video1_stream.m3u8"):
            return httpx.Response(200, text=_media(last=self.last))
        if path.endswith("init.mp4"):
            return httpx.Response(200, content=b"INIT")
        return httpx.Response(200, content=path.rsplit("/", 1)[-1].encode())


def _reader(relay: _Relay, decoded: list) -> tuple[StreamFrameReader, httpx.AsyncClient, StreamFrameHub]:
    async def resolve(camera_id: str) -> str:
        return BASE

    def decoder(init, segment, count, aspect):
        decoded.append((init, segment, count, aspect))
        return [b"newest-" + segment, b"older-" + segment][:count]

    hub = StreamFrameHub(resolve_url=resolve, decoder=decoder)
    client = httpx.AsyncClient(transport=httpx.MockTransport(relay.handler))
    return StreamFrameReader("dahua-channel-2", hub, resolve, lambda: client, decoder), client, hub


async def test_reader_decodes_only_the_newest_segment_and_keeps_it_in_memory():
    relay, decoded = _Relay(), []
    reader, client, hub = _reader(relay, decoded)
    hub.note_snapshot("dahua-channel-2", _scene(size=(1600, 900)))

    assert await reader.fetch_once(client) is True
    [(init, segment, count, aspect)] = decoded
    assert init == b"INIT"
    assert segment == b"abc_video1_seg9.mp4"
    assert count == settings.best_photo_frames
    # Aspect learned from a real 16:9 snapshot undoes the D1 squeeze.
    assert aspect == pytest.approx(16 / 9)
    assert reader.latest.frame == b"newest-abc_video1_seg9.mp4"
    assert reader.latest.seq == 1

    # Same newest segment: nothing re-downloaded or re-decoded.
    assert await reader.fetch_once(client) is False
    assert len(decoded) == 1
    assert reader.stats.unchanged == 1

    relay.last = 10
    assert await reader.fetch_once(client) is True
    assert reader.latest.segment.endswith("seg10.mp4")
    assert reader.latest.seq == 2
    # The init segment and the master playlist are fetched once per session.
    assert sum(path.endswith("init.mp4") for path in relay.requests) == 1
    assert sum(path.endswith("index.m3u8") for path in relay.requests) == 1
    await client.aclose()


async def test_reader_run_backs_off_and_recovers(monkeypatch):
    relay, decoded = _Relay(), []
    reader, client, hub = _reader(relay, decoded)
    monkeypatch.setattr(settings, "stream_sample_interval_seconds", 0.01)
    hub._touched["dahua-channel-2"] = time.monotonic()
    relay.fail = True

    sleeps: list[float] = []
    real_sleep = stream_frames.asyncio.sleep

    async def fake_sleep(delay):
        sleeps.append(delay)
        if len(sleeps) == 4:
            relay.fail = False
        if len(sleeps) >= 6:
            hub._touched.clear()  # idle: let the reader exit
        await real_sleep(0)

    monkeypatch.setattr(stream_frames.asyncio, "sleep", fake_sleep)
    await reader.run()

    assert reader.stats.failed >= 3
    assert sleeps[1] > sleeps[0]  # exponential backoff while failing
    assert reader.latest is not None and reader.latest.frame.startswith(b"newest-")


async def test_non_hls_streams_are_left_to_snapshots():
    async def resolve(camera_id: str) -> str:
        return "rtsp://100.64.0.1:8554/dahua-2"

    hub = StreamFrameHub(resolve_url=resolve)
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    reader = StreamFrameReader("dahua-channel-2", hub, resolve, lambda: client, lambda *a: [])
    hub._touched["dahua-channel-2"] = time.monotonic()
    await reader.run()
    assert reader.unsupported is True
    assert reader.latest is None


def test_stale_frames_are_never_returned(monkeypatch):
    hub = StreamFrameHub()
    hub._readers["cam"] = SimpleNamespace(
        latest=FrameSample((b"x",), time.monotonic() - 60, 1, "seg")
    )
    assert hub.latest("cam") is None
    hub._readers["cam"].latest = FrameSample((b"x",), time.monotonic(), 2, "seg")
    assert hub.latest("cam").frame == b"x"


def test_successful_hls_polls_are_not_logged_line_by_line():
    quiet = _QuietStreamPolling()

    def record(url: str, status: int) -> logging.LogRecord:
        return logging.LogRecord(
            "httpx", logging.INFO, __file__, 1, 'HTTP Request: %s %s "%s %d %s"',
            ("GET", url, "HTTP/1.1", status, "OK"), None,
        )

    assert quiet.filter(record("http://h/dahua-2/video1_stream.m3u8", 200)) is False
    assert quiet.filter(record("http://h/dahua-2/abc_seg9.mp4", 200)) is False
    assert quiet.filter(record("http://h/dahua-2/abc_seg9.mp4", 404)) is True
    assert quiet.filter(record("http://edge/channels/2/snapshot", 503)) is True


def test_decode_latest_frames_from_a_real_video():
    cv2 = pytest.importorskip("cv2")
    import os
    import tempfile

    import numpy as np

    handle, path = tempfile.mkstemp(suffix=".mp4")
    os.close(handle)
    try:
        writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), 20, (64, 48))
        for index in range(20):
            frame = np.zeros((48, 64, 3), dtype=np.uint8)
            frame[:, :] = (0, 0, 255) if index == 19 else (255, 0, 0)
            writer.write(frame)
        writer.release()
        with open(path, "rb") as video:
            data = video.read()
    finally:
        os.unlink(path)

    frames = stream_frames.decode_latest_frames(b"", data, 3, aspect_ratio=16 / 9)
    assert len(frames) == 3
    newest = cv2.imdecode(np.frombuffer(frames[0], np.uint8), cv2.IMREAD_COLOR)
    assert newest.shape[:2] == (48, 85)
    blue, green, red = newest[24, 42]
    assert red > 200 and blue < 60  # the last frame, not an older one


# --- ingestion ---------------------------------------------------------------


@pytest.fixture(autouse=True)
async def _clean(client):
    ingestion.reset_cooldowns()
    stream_hub._readers.clear()

    async def _clear():
        async with SessionLocal() as session:
            await session.execute(delete(AIAnalysis))
            await session.execute(delete(Activity))
            await session.execute(delete(Event))
            await session.commit()

    await _clear()
    yield
    await _clear()
    ingestion.reset_cooldowns()
    stream_hub._readers.clear()


@pytest.fixture
def streamed(monkeypatch):
    """Treat the mock provider as a stream camera and count CGI snapshots."""
    snapshots: list[str] = []
    fallback = _scene((CAR, CAR_RGB))

    async def snapshot(camera_id: str) -> bytes:
        snapshots.append(camera_id)
        return fallback

    monkeypatch.setattr(mock_provider, "supports_stream_frames", True, raising=False)
    monkeypatch.setattr(mock_provider, "get_snapshot", snapshot)
    monkeypatch.setattr(stream_hub, "ensure", lambda camera_id: None)

    def publish(frame: bytes, seq: int = 1, age: float = 0.0) -> None:
        stream_hub._readers[CAMERA] = SimpleNamespace(
            latest=FrameSample((frame, frame), time.monotonic() - age, seq, f"seg{seq}")
        )

    return SimpleNamespace(snapshots=snapshots, publish=publish)


async def _events(client) -> list[dict]:
    return [e for e in (await client.get("/api/v1/events")).json() if e["camera_id"] == CAMERA]


async def test_ingestion_uses_the_stream_frame_and_never_asks_the_nvr(client, streamed):
    streamed.publish(_scene((PERSON, PERSON_RGB), (CAR, CAR_RGB)))
    mock_detector().set_script(CAMERA, [CAR, PERSON])

    assert await ingestion.poll_once() >= 2

    events = {e["type"]: e for e in await _events(client)}
    assert set(events) >= {"person", "vehicle"}
    assert events["person"]["metadata"]["frame_source"] == "stream"
    assert events["person"]["metadata"]["best_photo"]["detection"]["label"] == "person"
    assert events["vehicle"]["metadata"]["frame_source"] == "stream"
    assert CAMERA not in streamed.snapshots  # neither detection nor photos hit CGI
    assert ingestion.frame_stats()[CAMERA]["stream"] == 1


async def test_the_same_stream_frame_is_not_detected_twice(client, streamed, monkeypatch):
    monkeypatch.setattr(settings, "event_cooldown_seconds", 0.0)
    streamed.publish(_scene((PERSON, PERSON_RGB)))
    mock_detector().set_script(CAMERA, [PERSON])

    await ingestion.poll_once()
    await ingestion.poll_once()
    assert len(await _events(client)) == 1
    assert ingestion.frame_stats()[CAMERA]["stream_repeat"] == 1

    streamed.publish(_scene((PERSON, PERSON_RGB)), seq=2)
    await ingestion.poll_once()
    assert len(await _events(client)) == 2


async def test_stale_stream_falls_back_to_a_rate_limited_snapshot(client, streamed, monkeypatch):
    monkeypatch.setattr(settings, "event_cooldown_seconds", 0.0)
    streamed.publish(_scene((PERSON, PERSON_RGB)), age=settings.stream_frame_max_age_seconds + 5)
    mock_detector().set_script(CAMERA, [CAR])

    await ingestion.poll_once()
    [event] = await _events(client)
    assert event["metadata"]["frame_source"] == "snapshot"
    assert streamed.snapshots.count(CAMERA) >= 1
    asked = streamed.snapshots.count(CAMERA)

    # Still stale a tick later: no second CGI request inside the interval.
    await ingestion.poll_once()
    assert streamed.snapshots.count(CAMERA) == asked
    assert ingestion.frame_stats()[CAMERA]["no_frame"] == 1


def test_default_sampling_is_a_few_seconds_for_stream_cameras():
    assert settings.stream_sample_interval_seconds <= 5
    assert ingestion.tick_seconds() == settings.stream_sample_interval_seconds
    # Snapshot-only cameras keep the old, NVR-friendly spacing.
    assert settings.event_poll_interval_seconds >= 20


# --- stationary suppression --------------------------------------------------


@pytest.fixture
def snapshot_camera(monkeypatch):
    monkeypatch.setattr(settings, "event_cooldown_seconds", 0.0)
    monkeypatch.setattr(settings, "event_poll_interval_seconds", 0.0)

    async def snapshot(camera_id: str) -> bytes:
        return _scene((PERSON, PERSON_RGB), (CAR, CAR_RGB))

    monkeypatch.setattr(mock_provider, "get_snapshot", snapshot)


async def test_a_parked_car_is_not_re_reported(client, snapshot_camera):
    mock_detector().set_script(CAMERA, [CAR])
    await ingestion.poll_once()
    # Jittered box and an extra partial box on the same car: same object.
    jitter = Detection("car", 0.9, BoundingBox(0.552, 0.405, 0.95, 0.84))
    partial = Detection("car", 0.51, BoundingBox(0.56, 0.45, 0.80, 0.80))
    mock_detector().set_script(CAMERA, [jitter, partial])
    await ingestion.poll_once()
    await ingestion.poll_once()

    assert [e["type"] for e in await _events(client)] == ["vehicle"]
    assert ingestion.frame_stats()[CAMERA]["stationary_suppressed"] == 2


async def test_a_flickering_static_box_does_not_re_emit_the_parked_car(client, snapshot_camera):
    # Live ch1: a parked car plus a half-out-of-frame car at the top edge
    # that the detector only sometimes finds. Each reappearance used to
    # count as "new" against the last event and re-emit every cooldown.
    edge = Detection("car", 0.6, BoundingBox(0.86, 0.0, 0.954, 0.084))
    far = Detection("car", 0.55, BoundingBox(0.13, 0.70, 0.22, 0.91))
    for frame in ([CAR], [CAR, edge], [CAR], [CAR, far], [CAR], [CAR, edge], [CAR, far], [CAR, edge, far]):
        mock_detector().set_script(CAMERA, frame)
        await ingestion.poll_once()

    # One event for the parked car, one each the first time the edge and
    # far boxes appeared; never again for either afterwards.
    assert [e["type"] for e in await _events(client)] == ["vehicle"] * 3
    assert ingestion.frame_stats()[CAMERA]["stationary_suppressed"] == 5


async def test_a_new_or_moving_vehicle_still_emits(client, snapshot_camera):
    mock_detector().set_script(CAMERA, [CAR])
    await ingestion.poll_once()
    arriving = Detection("car", 0.8, BoundingBox(0.05, 0.50, 0.35, 0.85))
    mock_detector().set_script(CAMERA, [CAR, arriving])
    await ingestion.poll_once()
    moved = Detection("car", 0.9, BoundingBox(0.30, 0.40, 0.70, 0.85))
    mock_detector().set_script(CAMERA, [moved])
    await ingestion.poll_once()

    assert [e["type"] for e in await _events(client)] == ["vehicle"] * 3


async def test_a_parked_car_is_reported_again_after_the_window(client, snapshot_camera, monkeypatch):
    mock_detector().set_script(CAMERA, [CAR])
    await ingestion.poll_once()
    monkeypatch.setattr(settings, "stationary_suppress_seconds", 0.0)
    await ingestion.poll_once()
    assert len(await _events(client)) == 2


async def test_people_are_never_suppressed_as_stationary(client, snapshot_camera):
    mock_detector().set_script(CAMERA, [CAR, PERSON])
    await ingestion.poll_once()
    await ingestion.poll_once()

    types = sorted(e["type"] for e in await _events(client))
    assert types == ["person", "person", "vehicle"]


# --- dashboard snapshot -------------------------------------------------------


async def test_dashboard_snapshot_serves_the_cached_stream_frame(client, streamed):
    streamed.publish(b"stream-jpeg")
    response = await client.get(f"/api/v1/cameras/{CAMERA}/snapshot")
    assert response.status_code == 200
    assert response.content == b"stream-jpeg"
    assert response.headers["x-frame-source"] == "stream"
    assert streamed.snapshots == []


async def test_dashboard_snapshot_falls_back_to_the_camera(client, streamed):
    response = await client.get(f"/api/v1/cameras/{CAMERA}/snapshot")
    assert response.status_code == 200
    assert response.headers["x-frame-source"] == "snapshot"
    assert streamed.snapshots == [CAMERA]
