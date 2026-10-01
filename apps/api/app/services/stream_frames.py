"""Latest-frame readers for cameras that expose an HLS sub-stream.

Why this exists: the Dahua NVR behind the edge connector sustains only ~1-2
concurrent CGI sessions. Measured in production, 69-82% of its 4K
``snapshot.cgi`` requests were refused, so ingestion got roughly one usable
frame per camera every ~100s and a person crossing the yard in 5-10s was
almost never looked at - only a car parked all day was.

The edge already relays each channel's H.264 sub-stream (704x576) through
MediaMTX as low-latency HLS, and the API already reaches it over the
tailnet for browser playback. MediaMTX holds a single RTSP session per
channel however many clients read it, so taking frames from there adds no
NVR CGI load at all.

One :class:`StreamFrameReader` per camera polls the media playlist every
``stream_sample_interval_seconds``, downloads only the newest complete
segment (each starts on a keyframe, ~2s), decodes it with OpenCV/FFmpeg and
keeps the last few frames in memory. Readers back off on failure, re-resolve
the stream URL after repeated failures, and stop when nobody has asked for a
frame in a while. Callers check frame age, so a stalled reader is never
mistaken for a live picture.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import tempfile
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Awaitable, Callable
from urllib.parse import urljoin, urlsplit

import httpx

from ..config import settings

logger = logging.getLogger(__name__)

# Spacing, in decoded frames, between the frames kept for best-photo
# selection (at 20fps: 0.2s apart, so the kept frames differ in blur/pose).
_FRAME_STRIDE = 4
_JPEG_QUALITY = 90
# Consecutive failures after which the stream URL is resolved again (the
# edge may have been restarted with a new MediaMTX address).
_RERESOLVE_AFTER_FAILURES = 3
_MAX_BACKOFF_SECONDS = 60.0
# Readers whose stream is not HLS are retried this rarely.
_UNSUPPORTED_RETRY_SECONDS = 600.0


class StreamUnavailableError(Exception):
    """The stream could not produce a frame right now."""


@dataclass(frozen=True)
class FrameSample:
    """The newest decoded frames of one stream segment, newest first."""

    frames: tuple[bytes, ...]
    captured_at: float
    seq: int
    segment: str

    @property
    def frame(self) -> bytes:
        return self.frames[0]

    def age(self, now: float | None = None) -> float:
        return (time.monotonic() if now is None else now) - self.captured_at


@dataclass
class ReaderStats:
    ok: int = 0
    unchanged: int = 0
    failed: int = 0
    last_error: str | None = None
    since: float = field(default_factory=time.monotonic)

    def as_dict(self) -> dict:
        attempts = self.ok + self.unchanged + self.failed
        return {
            "ok": self.ok,
            "unchanged": self.unchanged,
            "failed": self.failed,
            "success_percent": (
                round(100.0 * (self.ok + self.unchanged) / attempts, 1) if attempts else None
            ),
            "last_error": self.last_error,
        }


# --- playlist parsing ---------------------------------------------------------


def variant_playlist_url(text: str, base_url: str) -> str | None:
    """First variant of a master playlist, or ``None`` for a media playlist."""
    lines = [line.strip() for line in text.splitlines()]
    for index, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF"):
            for candidate in lines[index + 1:]:
                if candidate and not candidate.startswith("#"):
                    return urljoin(base_url, candidate)
    return None


def latest_segment(text: str, base_url: str) -> tuple[str | None, str | None]:
    """``(init_url, newest_complete_segment_url)`` of a media playlist.

    LL-HLS partial segments (``#EXT-X-PART``) and preload hints are ignored:
    only full segments are guaranteed to start on a keyframe. Segments
    marked ``#EXT-X-GAP`` (MediaMTX pads a freshly started muxer with them)
    carry no media and are skipped.
    """
    init_url: str | None = None
    newest: str | None = None
    gap = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MAP:"):
            uri = _attribute(line, "URI")
            if uri:
                init_url = urljoin(base_url, uri)
        elif line.startswith("#EXT-X-GAP"):
            gap = True
        elif line.startswith("#"):
            continue
        else:
            if not gap:
                newest = urljoin(base_url, line)
            gap = False
    return init_url, newest


def _attribute(line: str, name: str) -> str | None:
    marker = f'{name}="'
    start = line.find(marker)
    if start < 0:
        return None
    start += len(marker)
    end = line.find('"', start)
    return line[start:end] if end > start else None


# --- decoding -----------------------------------------------------------------


def decode_latest_frames(
    init: bytes, segment: bytes, count: int, aspect_ratio: float | None = None
) -> list[bytes]:
    """Decode an (fMP4 init + segment) and return up to ``count`` JPEGs.

    Frames are the last one of the segment and earlier ones ``_FRAME_STRIDE``
    apart, newest first. ``aspect_ratio`` (display width/height) undoes the
    anamorphic squeeze of a D1 sub-stream so photos are not distorted.
    """
    import cv2

    count = max(1, count)
    keep: deque = deque(maxlen=(count - 1) * _FRAME_STRIDE + 1)
    handle, path = tempfile.mkstemp(suffix=".mp4")
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(init)
            out.write(segment)
        capture = cv2.VideoCapture(path)
        try:
            while True:
                ok, frame = capture.read()
                if not ok:
                    break
                keep.append(frame)
        finally:
            capture.release()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if not keep:
        raise StreamUnavailableError("segment contained no decodable frames")

    frames = list(keep)[::-1][::_FRAME_STRIDE][:count]
    encoded: list[bytes] = []
    for frame in frames:
        height, width = frame.shape[:2]
        if aspect_ratio:
            target_width = int(round(height * aspect_ratio))
            if abs(target_width - width) > 2:
                frame = cv2.resize(frame, (target_width, height), interpolation=cv2.INTER_LINEAR)
        ok, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), _JPEG_QUALITY])
        if ok:
            encoded.append(buffer.tobytes())
    if not encoded:
        raise StreamUnavailableError("could not encode decoded frame")
    return encoded


def image_aspect_ratio(image: bytes) -> float | None:
    """Width/height of an encoded image, read from its header only."""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(image)) as opened:
            width, height = opened.size
    except Exception:  # noqa: BLE001 - an unreadable image just teaches nothing
        return None
    return width / height if width and height else None


# --- readers ------------------------------------------------------------------

UrlResolver = Callable[[str], Awaitable[str]]
Decoder = Callable[[bytes, bytes, int, "float | None"], list[bytes]]


async def _resolve_live_url(camera_id: str) -> str:
    from ..providers.base import ProviderUnavailableError
    from .provider_registry import find_provider_for_camera

    provider = await find_provider_for_camera(camera_id)
    if provider is None:
        raise ProviderUnavailableError(f"no provider for {camera_id}")
    return await provider.get_live_stream(camera_id)


def _default_client() -> httpx.AsyncClient:
    options: dict = {"timeout": 10.0, "follow_redirects": True}
    proxy_url = os.environ.get("TAILSCALE_HTTP_PROXY")
    if proxy_url:
        options["proxy"] = proxy_url
    return httpx.AsyncClient(**options)


class StreamFrameReader:
    def __init__(
        self,
        camera_id: str,
        hub: "StreamFrameHub",
        resolve_url: UrlResolver,
        client_factory: Callable[[], httpx.AsyncClient],
        decoder: Decoder,
    ) -> None:
        self.camera_id = camera_id
        self.hub = hub
        self._resolve_url = resolve_url
        self._client_factory = client_factory
        self._decoder = decoder
        self.latest: FrameSample | None = None
        self.stats = ReaderStats()
        self.unsupported = False
        self._manifest_url: str | None = None
        self._media_url: str | None = None
        self._init: tuple[str, bytes] | None = None
        self._seq = 0
        self._failures = 0

    async def run(self) -> None:
        client = self._client_factory()
        try:
            while not self.hub.idle(self.camera_id):
                delay = self.hub.sample_interval(self.camera_id)
                try:
                    await self.fetch_once(client)
                    self._failures = 0
                except asyncio.CancelledError:
                    raise
                except _Unsupported as exc:
                    self.unsupported = True
                    logger.info("stream frames %s: not readable (%s); using snapshots", self.camera_id, exc)
                    return
                except Exception as exc:  # noqa: BLE001 - a reader must never die on one bad fetch
                    self._failures += 1
                    self.stats.failed += 1
                    self.stats.last_error = f"{type(exc).__name__}: {exc}"[:200]
                    if self._failures >= _RERESOLVE_AFTER_FAILURES:
                        self._manifest_url = None
                        self._media_url = None
                        self._init = None
                    delay = min(
                        _MAX_BACKOFF_SECONDS,
                        settings.stream_sample_interval_seconds * (2 ** min(self._failures - 1, 5)),
                    )
                self.hub.maybe_log_stats(self)
                await asyncio.sleep(delay)
        finally:
            await client.aclose()

    async def fetch_once(self, client: httpx.AsyncClient) -> bool:
        """Fetch the newest segment; ``True`` if it produced a new sample."""
        if self._manifest_url is None:
            url = await self._resolve_url(self.camera_id)
            if ".m3u8" not in urlsplit(url).path:
                raise _Unsupported(f"live stream is not HLS: {urlsplit(url).scheme}")
            self._manifest_url = url
            self._media_url = None
        if self._media_url is None:
            text = await _get_text(client, self._manifest_url)
            self._media_url = variant_playlist_url(text, self._manifest_url) or self._manifest_url
            playlist = text if self._media_url == self._manifest_url else None
        else:
            playlist = None
        if playlist is None:
            playlist = await _get_text(client, self._media_url)

        init_url, segment_url = latest_segment(playlist, self._media_url)
        if segment_url is None:
            raise StreamUnavailableError("playlist has no complete segment yet")
        if self.latest is not None and self.latest.segment == segment_url:
            self.stats.unchanged += 1
            return False

        init = b""
        if init_url:
            if self._init is None or self._init[0] != init_url:
                self._init = (init_url, await _get_bytes(client, init_url))
            init = self._init[1]
        segment = await _get_bytes(client, segment_url)
        frames = await asyncio.to_thread(
            self._decoder,
            init,
            segment,
            settings.best_photo_frames,
            self.hub.aspect_ratio(self.camera_id),
        )
        self._seq += 1
        self.latest = FrameSample(tuple(frames), time.monotonic(), self._seq, segment_url)
        self.stats.ok += 1
        return True


class _Unsupported(Exception):
    pass


async def _get_text(client: httpx.AsyncClient, url: str) -> str:
    response = await client.get(url)
    if response.status_code >= 400:
        raise StreamUnavailableError(f"HTTP {response.status_code} for playlist")
    return response.text


async def _get_bytes(client: httpx.AsyncClient, url: str) -> bytes:
    response = await client.get(url)
    if response.status_code >= 400:
        raise StreamUnavailableError(f"HTTP {response.status_code} for segment")
    if not response.content:
        raise StreamUnavailableError("empty segment")
    return response.content


class StreamFrameHub:
    """Owns one reader task per camera and answers "latest fresh frame?"."""

    def __init__(
        self,
        resolve_url: UrlResolver = _resolve_live_url,
        client_factory: Callable[[], httpx.AsyncClient] = _default_client,
        decoder: Decoder = decode_latest_frames,
    ) -> None:
        self._resolve_url = resolve_url
        self._client_factory = client_factory
        self._decoder = decoder
        self._readers: dict[str, StreamFrameReader] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._touched: dict[str, float] = {}
        self._aspect: dict[str, float] = {}
        self._unsupported_at: dict[str, float] = {}
        self._stats_logged_at: dict[str, float] = {}
        self._boost_until: dict[str, float] = {}

    def boost(self, camera_id: str, seconds: float) -> bool:
        """Sample ``camera_id`` every ``mailbox_boost_interval_seconds`` for
        ``seconds`` (e.g. while a person is at the mailbox).

        Only a camera with a working relayed-stream reader is boosted: that
        costs the NVR nothing extra. Snapshot-only cameras keep their
        ``event_poll_interval_seconds`` budget. Returns whether it applied.
        """
        reader = self._readers.get(camera_id)
        if seconds <= 0 or reader is None or getattr(reader, "unsupported", False):
            return False
        until = time.monotonic() + seconds
        if until > self._boost_until.get(camera_id, 0.0):
            self._boost_until[camera_id] = until
        return True

    def clear_boosts(self) -> None:
        self._boost_until.clear()

    def release(self, camera_id: str) -> None:
        """Stop reading ``camera_id`` (this replica no longer ingests it), so
        a standby replica never holds an extra NVR stream session."""
        task = self._tasks.pop(camera_id, None)
        if task is not None and not task.done():
            task.cancel()
        self._readers.pop(camera_id, None)
        self._touched.pop(camera_id, None)
        self._boost_until.pop(camera_id, None)

    def boosted(self, camera_id: str | None = None) -> bool:
        """Whether ``camera_id`` (or, with ``None``, any camera) is boosted."""
        now = time.monotonic()
        if camera_id is not None:
            return self._boost_until.get(camera_id, 0.0) > now
        return any(until > now for until in self._boost_until.values())

    def sample_interval(self, camera_id: str) -> float:
        base = settings.stream_sample_interval_seconds
        if self.boosted(camera_id):
            return min(base, settings.mailbox_boost_interval_seconds)
        return base

    def ensure(self, camera_id: str) -> None:
        """Start a reader for ``camera_id`` unless one is already running."""
        self._touched[camera_id] = time.monotonic()
        task = self._tasks.get(camera_id)
        if task is not None and not task.done():
            return
        reader = self._readers.get(camera_id)
        if reader is not None and reader.unsupported:
            failed_at = self._unsupported_at.setdefault(camera_id, time.monotonic())
            if time.monotonic() - failed_at < _UNSUPPORTED_RETRY_SECONDS:
                return
        self._unsupported_at.pop(camera_id, None)
        _install_log_filter()
        reader = StreamFrameReader(
            camera_id, self, self._resolve_url, self._client_factory, self._decoder
        )
        self._readers[camera_id] = reader
        self._tasks[camera_id] = asyncio.create_task(
            reader.run(), name=f"stream-frames-{camera_id}"
        )

    def latest(self, camera_id: str, max_age: float | None = None) -> FrameSample | None:
        """Newest sample if it is fresh enough, else ``None``. Never blocks."""
        reader = self._readers.get(camera_id)
        if reader is None or reader.latest is None:
            return None
        limit = settings.stream_frame_max_age_seconds if max_age is None else max_age
        return reader.latest if reader.latest.age() <= limit else None

    def idle(self, camera_id: str) -> bool:
        touched = self._touched.get(camera_id)
        return touched is None or time.monotonic() - touched > settings.stream_reader_idle_seconds

    def note_snapshot(self, camera_id: str, image: bytes) -> None:
        """Learn a camera's true display aspect from one of its real snapshots."""
        ratio = image_aspect_ratio(image)
        if ratio:
            self._aspect[camera_id] = ratio

    def aspect_ratio(self, camera_id: str) -> float | None:
        return settings.stream_frame_aspect_ratio or self._aspect.get(camera_id)

    def stats(self) -> dict[str, dict]:
        return {camera_id: reader.stats.as_dict() for camera_id, reader in self._readers.items()}

    def maybe_log_stats(self, reader: StreamFrameReader) -> None:
        now = time.monotonic()
        last = self._stats_logged_at.setdefault(reader.camera_id, now)
        if now - last < settings.ingestion_stats_log_seconds:
            return
        stats = reader.stats
        summary = stats.as_dict()
        window = now - stats.since
        logger.info(
            "stream frames %s: window=%.0fs new=%d unchanged=%d failed=%d success=%s%% "
            "cadence=%s last_error=%s",
            reader.camera_id,
            window,
            stats.ok,
            stats.unchanged,
            stats.failed,
            summary["success_percent"] if summary["success_percent"] is not None else "n/a",
            f"{window / stats.ok:.1f}s" if stats.ok else "n/a",
            stats.last_error,
        )
        reader.stats = ReaderStats()
        self._stats_logged_at[reader.camera_id] = now

    async def stop(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks.clear()
        self._readers.clear()
        self._touched.clear()
        self._boost_until.clear()


class _QuietStreamPolling(logging.Filter):
    """Drop httpx's per-request INFO line for successful HLS fetches.

    Readers fetch a playlist and a segment every few seconds per camera;
    logging each would drown every other line. Failures (>=400) and all
    non-HLS requests are still logged, and readers log their own periodic
    success-rate summary instead.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args if isinstance(record.args, tuple) else ()
        if len(args) >= 4:
            url = str(args[1])
            path = urlsplit(url).path
            if path.endswith((".m3u8", ".mp4")):
                try:
                    return int(args[3]) >= 400
                except (TypeError, ValueError):
                    return True
        return True


_filter_installed = False


def _install_log_filter() -> None:
    global _filter_installed
    if not _filter_installed:
        logging.getLogger("httpx").addFilter(_QuietStreamPolling())
        _filter_installed = True


stream_hub = StreamFrameHub()
