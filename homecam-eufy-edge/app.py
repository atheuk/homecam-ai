"""HomeCam Eufy edge adapter.

Translates HomeCam's small REST adapter contract (see ``docs/eufy.md``)
onto the `eufy-security-ws` WebSocket bridge, and hands live video to
go2rtc so the browser gets playable HLS instead of raw P2P H.264.

Security boundary: this process runs on the owner's Home Assistant host,
alongside the Eufy bridge. Eufy account credentials, 2FA/captcha state and
the persistent Eufy session live in the *bridge*, never here and never in
HomeCam core. HomeCam authenticates to this adapter with its own bearer
token, which is unrelated to any Eufy credential.

Deliberate battery behaviour: the T8210 is battery powered. Snapshots are
served from the bridge's last event image and never wake the device;
livestreams are stopped as soon as the last viewer disconnects.
"""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import os
import re
import secrets
import struct
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import quote

from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from eufy_ws import EufyBridgeError, EufyBridgeUnavailable, EufyWsClient

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("eufy-edge")

JPEG_START_OF_IMAGE = b"\xff\xd8"

_SECRET_IN_PATH = re.compile(
    r"(/hls/)[^/\s\"?]+(?=/eufy-)"  # relay path token
    r"|((?:\?|&|%3F|%26)token(?:=|%3D))[^&%\s\"#]+",  # ingest token, raw or URL-encoded
    re.IGNORECASE,
)


def redact(text: str) -> str:
    """Strip the HLS path token and ffmpeg ingest token from a log line."""
    return _SECRET_IN_PATH.sub(lambda m: f"{m.group(1) or m.group(2)}***", text)


class RedactSecretsFilter(logging.Filter):
    """uvicorn's access log records the full request target, which carries
    the HLS relay path token and the ``/internal`` ingest token."""

    def filter(self, record: logging.LogRecord) -> bool:
        # Keep the args tuple shape: uvicorn's AccessFormatter unpacks it.
        if isinstance(record.args, tuple):
            record.args = tuple(_redact_arg(a) for a in record.args)
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        return True


def _redact_arg(value):
    if isinstance(value, (int, float)) or value is None:
        return value
    text = str(value)  # e.g. httpx.URL objects in httpx's request log
    cleaned = redact(text)
    return value if cleaned == text else cleaned


for _name in ("uvicorn.access", "uvicorn.error", "httpx"):
    logging.getLogger(_name).addFilter(RedactSecretsFilter())


@dataclass
class Settings:
    eufy_ws_url: str
    edge_token: str | None
    go2rtc_url: str
    go2rtc_public_url: str
    self_url: str
    stream_token: str
    live_ready_timeout: float
    live_idle_stop_seconds: float = 60.0
    # When set, ``/live`` advertises the adapter's own token-gated HLS relay
    # (``{base}/hls/{hls_token}/...``) instead of go2rtc directly, so go2rtc
    # can stay bound to loopback (the Home Assistant add-on layout).
    hls_public_base_url: str | None = None
    hls_token: str = ""
    # Set when go2rtc runs in the same network namespace (HA add-on): the
    # raw ingest endpoint then refuses anything but loopback clients.
    internal_loopback_only: bool = False
    # Bounded post-trigger event clips. The doorbell sleeps, so there is no
    # pre-roll: recording starts when the bridge reports motion/person/ring.
    event_clips_enabled: bool = True
    event_clip_seconds: int = 15
    event_clip_cooldown_seconds: float = 120.0
    event_clip_daily_limit: int = 24

    @classmethod
    def from_env(cls) -> "Settings":
        go2rtc_url = os.environ.get("GO2RTC_URL", "http://127.0.0.1:1984").rstrip("/")
        return cls(
            eufy_ws_url=os.environ.get("EUFY_WS_URL", "ws://127.0.0.1:3000"),
            edge_token=os.environ.get("HOME_CAM_EUFY_TOKEN") or None,
            go2rtc_url=go2rtc_url,
            # Where the *browser/Azure* should fetch HLS from. Defaults to the
            # same address we use internally, which is only correct when the
            # adapter and go2rtc share a host reachable over the overlay net.
            go2rtc_public_url=(os.environ.get("GO2RTC_PUBLIC_URL") or go2rtc_url).rstrip("/"),
            # How go2rtc's ffmpeg reaches *us* to pull the raw H.264 feed.
            self_url=os.environ.get("SELF_URL", "http://127.0.0.1:8091").rstrip("/"),
            stream_token=os.environ.get("STREAM_TOKEN") or secrets.token_urlsafe(24),
            live_ready_timeout=float(os.environ.get("LIVE_READY_TIMEOUT_SECONDS", "15")),
            live_idle_stop_seconds=float(os.environ.get("LIVE_IDLE_STOP_SECONDS", "60")),
            hls_public_base_url=(os.environ.get("HLS_PUBLIC_BASE_URL") or "").rstrip("/") or None,
            hls_token=os.environ.get("HLS_TOKEN") or secrets.token_urlsafe(24),
            internal_loopback_only=os.environ.get("INTERNAL_LOOPBACK_ONLY", "").lower() in ("1", "true", "yes"),
            event_clips_enabled=os.environ.get("EVENT_CLIPS_ENABLED", "true").lower() in ("1", "true", "yes"),
            event_clip_seconds=max(5, min(30, int(os.environ.get("EVENT_CLIP_SECONDS", "15")))),
            event_clip_cooldown_seconds=max(30.0, float(os.environ.get("EVENT_CLIP_COOLDOWN_SECONDS", "120"))),
            event_clip_daily_limit=max(0, min(96, int(os.environ.get("EVENT_CLIP_DAILY_LIMIT", "24")))),
        )


settings = Settings.from_env()
client = EufyWsClient(settings.eufy_ws_url, live_idle_stop_seconds=settings.live_idle_stop_seconds)

CLIP_MAX_BYTES = 8 * 1024 * 1024
CLIPS_KEPT_PER_DEVICE = 10
CLIP_TTL_SECONDS = 30 * 60


@dataclass
class EdgeClip:
    id: str
    serial: str
    trigger: str
    started_at: datetime
    monotonic: float
    data: bytes = b""
    duration_seconds: float | None = None
    complete: bool = False
    failed: str | None = None

    def summary(self) -> dict:
        return {
            "id": self.id,
            "started_at": self.started_at.isoformat(),
            "trigger": self.trigger,
            "duration_seconds": self.duration_seconds,
            "size_bytes": len(self.data),
            # Nothing before the trigger exists: the doorbell was asleep.
            "pre_roll_seconds": 0.0,
            "complete": self.complete,
            "failed": self.failed,
        }


def _mp4_complete_prefix(data: bytes) -> bytes:
    """Drop a trailing partial top-level box (a cut-off fragment)."""
    offset = 0
    while offset + 8 <= len(data):
        size = struct.unpack(">I", data[offset:offset + 4])[0]
        if size == 1 and offset + 16 <= len(data):
            size = struct.unpack(">Q", data[offset + 8:offset + 16])[0]
        if size < 8 or offset + size > len(data):
            break
        offset += size
    return data[:offset]


def _mp4_duration(data: bytes) -> float | None:
    """Sum fragment durations (trun sample durations / mdhd timescale).

    Best effort only: HomeCam re-validates every clip it stores.
    """
    timescale = None
    total = 0
    default_duration = 0

    def walk(start: int, end: int) -> None:
        nonlocal timescale, total, default_duration
        offset = start
        while offset + 8 <= end:
            size, kind = struct.unpack(">I4s", data[offset:offset + 8])
            if size < 8 or offset + size > end:
                return
            body = offset + 8
            if kind in (b"moov", b"trak", b"mdia", b"moof", b"traf"):
                walk(body, offset + size)
            elif kind == b"mdhd" and timescale is None:
                version = data[body]
                timescale = struct.unpack(">I", data[body + (20 if version == 1 else 12):][:4])[0]
            elif kind == b"tfhd":
                flags = int.from_bytes(data[body + 1:body + 4], "big")
                pos = body + 8
                if flags & 0x1:
                    pos += 8
                if flags & 0x2:
                    pos += 4
                if flags & 0x8:
                    default_duration = struct.unpack(">I", data[pos:pos + 4])[0]
            elif kind == b"trun":
                flags = int.from_bytes(data[body + 1:body + 4], "big")
                count = struct.unpack(">I", data[body + 4:body + 8])[0]
                pos = body + 8 + (4 if flags & 0x1 else 0) + (4 if flags & 0x4 else 0)
                per = sum(4 for bit in (0x100, 0x200, 0x400, 0x800) if flags & bit)
                for _ in range(min(count, 100000)):
                    total += struct.unpack(">I", data[pos:pos + 4])[0] if flags & 0x100 else default_duration
                    pos += per
            offset += size

    try:
        walk(0, len(data))
    except (struct.error, IndexError):
        return None
    if not timescale or total <= 0:
        return None
    return round(total / timescale, 2)


@dataclass
class ClipRecorder:
    """Bounded, event-triggered post-roll recorder for battery Eufy devices.

    Never streams continuously: one short recording per trigger, a cooldown
    per device, a daily cap, a small in-memory ring buffer with a TTL, and the
    livestream is released (and the device allowed to sleep) as soon as the
    recording ends.
    """

    enabled: bool
    seconds: int
    cooldown: float
    daily_limit: int
    clips: dict[str, deque] = field(default_factory=dict)
    _last: dict[str, float] = field(default_factory=dict)
    _day: str = ""
    _today: int = 0
    _tasks: set = field(default_factory=set)

    def _prune(self) -> None:
        cutoff = time.monotonic() - CLIP_TTL_SECONDS
        for queue in self.clips.values():
            while queue and queue[0].monotonic < cutoff:
                queue.popleft()

    def list(self, serial: str) -> list[dict]:
        self._prune()
        return [clip.summary() for clip in self.clips.get(serial, ())]

    def get(self, serial: str, clip_id: str) -> EdgeClip | None:
        self._prune()
        return next((c for c in self.clips.get(serial, ()) if c.id == clip_id), None)

    def trigger(self, serial: str, trigger: str) -> EdgeClip | None:
        if not self.enabled:
            return None
        now = time.monotonic()
        if any(not c.complete and not c.failed for c in self.clips.get(serial, ())):
            return None  # already recording this device: that clip covers it
        if now - self._last.get(serial, -1e9) < self.cooldown:
            return None
        day = datetime.now(timezone.utc).date().isoformat()
        if day != self._day:
            self._day, self._today = day, 0
        if self._today >= self.daily_limit:
            logger.info("event clip daily limit reached; not waking the device")
            return None
        self._today += 1
        self._last[serial] = now
        clip = EdgeClip(uuid.uuid4().hex, serial, trigger, datetime.now(timezone.utc), now)
        self.clips.setdefault(serial, deque(maxlen=CLIPS_KEPT_PER_DEVICE)).append(clip)
        task = asyncio.get_running_loop().create_task(self._record(clip))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return clip

    async def _record(self, clip: EdgeClip) -> None:
        try:
            stream = await client.start_livestream(clip.serial)
            # A sleeping doorbell needs a few seconds of P2P wake-up before
            # its first SPS/PPS+IDR. Asking go2rtc to record earlier makes
            # its ffmpeg probe fail ("Invalid data found when processing
            # input"), so wait -- bounded -- for a decodable start first.
            if not await stream.wait_for_keyframe(settings.live_ready_timeout):
                if not stream.queues:
                    await client.stop_livestream(clip.serial)
                raise RuntimeError(
                    f"no keyframe from the device within {settings.live_ready_timeout:.0f}s"
                )
            name = await _ensure_go2rtc_stream(clip.serial)
            clip.data = await _fetch_mp4(name, self.seconds)
            if len(clip.data) < 64 or clip.data[4:8] != b"ftyp":
                raise RuntimeError("no video arrived from the livestream")
            clip.duration_seconds = _mp4_duration(clip.data)
            clip.complete = True
            logger.info("recorded a %s event clip (%d bytes)", clip.trigger, len(clip.data))
        except asyncio.CancelledError:
            clip.failed = "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001 - reported in the clip listing
            clip.failed = redact(str(exc) or type(exc).__name__)[:200]
            clip.data = b""
            logger.warning("event clip recording failed: %s", clip.failed)

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(BaseException):
                await task


async def _fetch_mp4(name: str, seconds: int) -> bytes:
    """Pull ``seconds`` of fragmented MP4 from the private go2rtc.

    go2rtc ends the response itself after ``duration``; a client-side
    deadline and byte cap bound it anyway. Closing the response drops the
    go2rtc consumer, which stops ffmpeg, which releases the livestream.
    """
    buffer = bytearray()
    deadline = time.monotonic() + seconds + settings.live_ready_timeout + 10
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=seconds + settings.live_ready_timeout)) as http:
        async with http.stream(
            "GET",
            f"{settings.go2rtc_url}/api/stream.mp4",
            params={"src": name, "duration": str(seconds)},
        ) as response:
            if response.status_code >= 400:
                raise RuntimeError(f"go2rtc refused the MP4 recording: HTTP {response.status_code}")
            async for chunk in response.aiter_bytes():
                buffer.extend(chunk)
                if len(buffer) >= CLIP_MAX_BYTES or time.monotonic() > deadline:
                    break
    return _mp4_complete_prefix(bytes(buffer[:CLIP_MAX_BYTES]))


recorder = ClipRecorder(
    enabled=settings.event_clips_enabled,
    seconds=settings.event_clip_seconds,
    cooldown=settings.event_clip_cooldown_seconds,
    daily_limit=settings.event_clip_daily_limit,
)
client.trigger_listeners.append(recorder.trigger)


@asynccontextmanager
async def lifespan(_: FastAPI):
    if settings.event_clips_enabled:
        # Event clips need the bridge's push events even when nobody has
        # called the API yet since the adapter started.
        with contextlib.suppress(Exception):
            await client.ensure_connected()
    yield
    await recorder.close()
    # Close the bridge socket and stop any livestream still running, so the
    # battery doorbell is not left awake by an adapter restart.
    await client.close()


app = FastAPI(title="HomeCam Eufy edge adapter", lifespan=lifespan)


def require_token(authorization: str | None) -> None:
    if not settings.edge_token:
        # No token configured: refuse everything rather than silently
        # running unauthenticated, even on a private network.
        raise HTTPException(503, "Eufy adapter has no HOME_CAM_EUFY_TOKEN configured")
    if not secrets.compare_digest(authorization or "", f"Bearer {settings.edge_token}"):
        raise HTTPException(401, "Missing or invalid bearer token")


async def _known_serial(device_id: str) -> str:
    try:
        await client.ensure_connected()
    except EufyBridgeUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    if not client.has_device(device_id):
        raise HTTPException(404, f"Unknown Eufy device '{device_id}'")
    return device_id


@app.get("/healthz")
async def healthz() -> dict:
    """Unauthenticated liveness probe only (process is up); never reports
    Eufy reachability, account state or any configuration detail."""
    return {"status": "ok"}


@app.get("/health")
async def health(authorization: str | None = Header(default=None)) -> JSONResponse:
    require_token(authorization)
    try:
        await client.ensure_connected()
    except EufyBridgeUnavailable as exc:
        # Report rather than raise: HomeCam renders auth_state to tell the
        # owner that local action (2FA/captcha/bridge restart) is needed.
        return JSONResponse({"auth_state": "unauthenticated", "detail": str(exc)})
    return JSONResponse({"auth_state": client.auth_state})


@app.get("/devices")
async def devices(authorization: str | None = Header(default=None)) -> dict:
    require_token(authorization)
    try:
        await client.ensure_connected()
    except EufyBridgeUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    devices = client.devices()
    for device in devices:
        device["capabilities"]["eventClips"] = settings.event_clips_enabled and device["capabilities"].get("liveStream", False)
    return {"devices": devices}


@app.get("/devices/{device_id}/clips")
async def event_clips(device_id: str, authorization: str | None = Header(default=None)) -> dict:
    require_token(authorization)
    if not settings.event_clips_enabled:
        raise HTTPException(404, "Event clips are disabled on this adapter")
    serial = await _known_serial(device_id)
    return {"clips": recorder.list(serial)}


@app.get("/devices/{device_id}/clips/{clip_id}")
async def event_clip(device_id: str, clip_id: str, authorization: str | None = Header(default=None)) -> Response:
    require_token(authorization)
    if not settings.event_clips_enabled:
        raise HTTPException(404, "Event clips are disabled on this adapter")
    serial = await _known_serial(device_id)
    clip = recorder.get(serial, clip_id)
    if clip is None or not clip.complete or not clip.data:
        raise HTTPException(404, "Clip not found")
    return Response(clip.data, media_type="video/mp4", headers={"Cache-Control": "no-store"})


@app.get("/devices/{device_id}/snapshot")
async def snapshot(device_id: str, authorization: str | None = Header(default=None)) -> Response:
    require_token(authorization)
    serial = await _known_serial(device_id)
    try:
        image = await client.snapshot(serial)
    except EufyBridgeUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except EufyBridgeError as exc:
        raise HTTPException(404, str(exc)) from exc
    if not image.startswith(JPEG_START_OF_IMAGE):
        raise HTTPException(502, "Eufy bridge returned a non-JPEG event image")
    return Response(image, media_type="image/jpeg")


def _stream_name(serial: str) -> str:
    return f"eufy-{serial}"


async def _ensure_go2rtc_stream(serial: str) -> str:
    """Register a go2rtc stream that pulls our raw H.264.

    go2rtc runs ffmpeg against our ``/internal`` endpoint and remuxes -- no
    transcoding, so this is cheap even on a Raspberry Pi.

    Uses ``PATCH /api/streams`` (go2rtc >= 1.2.0) on *every* call: it is
    idempotent, creates the stream when missing and is memory-only. ``PUT``
    is never used because it also writes the source -- including the stream
    token -- into go2rtc.yaml via ``app.PatchConfig``. A process-local
    "already registered" cache would go stale whenever go2rtc restarts.

    go2rtc < 1.2.0 has no PATCH handler and answers it with HTTP 200 without
    doing anything, so the registration is confirmed with a side-effect-free
    ``GET /api/streams?src=<name>`` (404 or ``null`` when missing).
    """
    name = _stream_name(serial)
    source = (
        f"ffmpeg:{settings.self_url}/internal/devices/{quote(serial)}/h264"
        f"?token={quote(settings.stream_token)}#video=copy"
    )
    api = f"{settings.go2rtc_url}/api/streams"
    try:
        async with httpx.AsyncClient(timeout=10.0) as http:
            response = await http.patch(api, params={"name": name, "src": source})
            if response.status_code < 400:
                check = await http.get(api, params={"src": name})
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise HTTPException(503, f"go2rtc is unreachable at {settings.go2rtc_url}: {exc}") from exc
    if response.status_code in (404, 405, 501):
        raise HTTPException(502, GO2RTC_UNSUPPORTED)
    if response.status_code >= 400:
        raise HTTPException(502, f"go2rtc rejected stream registration: HTTP {response.status_code}")
    if not _go2rtc_has_stream(check):
        raise HTTPException(502, GO2RTC_UNSUPPORTED)
    return name


GO2RTC_UNSUPPORTED = (
    "go2rtc did not register the stream; go2rtc >= 1.2.0 (PATCH /api/streams) is required"
)


def _go2rtc_has_stream(response: httpx.Response) -> bool:
    if response.status_code >= 400:
        return False
    try:
        return response.json() is not None
    except ValueError:
        return False


@app.get("/devices/{device_id}/live")
async def live(device_id: str, authorization: str | None = Header(default=None)) -> dict:
    require_token(authorization)
    serial = await _known_serial(device_id)
    try:
        await client.start_livestream(serial)
    except EufyBridgeUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except EufyBridgeError as exc:
        raise HTTPException(502, f"Eufy bridge could not start the livestream: {exc}") from exc
    name = await _ensure_go2rtc_stream(serial)
    if settings.hls_public_base_url:
        return {
            "hls_url": (
                f"{settings.hls_public_base_url}/hls/{quote(settings.hls_token, safe='')}"
                f"/{quote(name, safe='')}/stream.m3u8"
            )
        }
    return {"hls_url": f"{settings.go2rtc_public_url}/api/stream.m3u8?src={quote(name)}"}


# Exactly the child resources go2rtc's HLS module serves (internal/hls).
HLS_CHILD_FILES = frozenset({"playlist.m3u8", "segment.ts", "segment.m4s", "init.mp4"})
HLS_FORWARD_HEADERS = ("content-type", "cache-control")


async def _hls_relay(hls_token: str, name: str, target: str, params: list[tuple[str, str]]) -> Response:
    # Bearer headers cannot be attached by HLS players or the HomeCam API's
    # HLS relay, so the read-only video path is gated by a separate random
    # path token instead. It grants no access to devices, snapshots or the
    # bridge, and is never logged.
    if not secrets.compare_digest(hls_token, settings.hls_token):
        raise HTTPException(404, "Not found")
    prefix = "eufy-"
    if not name.startswith(prefix):
        raise HTTPException(404, "Not found")
    await _known_serial(name[len(prefix):])
    try:
        async with httpx.AsyncClient(timeout=20.0) as http:
            upstream = await http.get(f"{settings.go2rtc_url}{target}", params=params)
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise HTTPException(503, "go2rtc is unreachable") from exc
    headers = {k: upstream.headers[k] for k in HLS_FORWARD_HEADERS if k in upstream.headers}
    return Response(upstream.content, status_code=upstream.status_code, headers=headers)


@app.get("/hls/{hls_token}/{name}/stream.m3u8")
async def hls_manifest(hls_token: str, name: str, request: Request) -> Response:
    params = [(k, v) for k, v in request.query_params.multi_items() if k != "src"]
    return await _hls_relay(hls_token, name, "/api/stream.m3u8", [("src", name), *params])


@app.get("/hls/{hls_token}/{name}/hls/{file}")
async def hls_child(hls_token: str, name: str, file: str, request: Request) -> Response:
    if file not in HLS_CHILD_FILES:
        raise HTTPException(404, "Not found")
    params = list(request.query_params.multi_items())
    return await _hls_relay(hls_token, name, f"/api/hls/{file}", params)


@app.get("/internal/devices/{device_id}/h264")
async def internal_h264(device_id: str, request: Request, token: str = Query(default="")) -> StreamingResponse:
    """Raw H.264 Annex-B byte stream for go2rtc's ffmpeg source.

    Authenticated with a separate single-purpose token passed in the query
    string, because ffmpeg sources cannot easily set request headers. This
    endpoint is never exposed to HomeCam or the browser.
    """
    if settings.internal_loopback_only and not _is_loopback(request.client.host if request.client else ""):
        raise HTTPException(403, "Internal endpoint is loopback-only")
    if not secrets.compare_digest(token, settings.stream_token):
        raise HTTPException(401, "Invalid stream token")
    serial = await _known_serial(device_id)
    try:
        queue = await client.subscribe(serial)
    except EufyBridgeUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except EufyBridgeError as exc:
        raise HTTPException(502, str(exc)) from exc

    async def iterator():
        try:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    return
                yield chunk
        finally:
            # Runs when ffmpeg disconnects, which is what releases the P2P
            # session and lets the doorbell go back to sleep.
            with contextlib.suppress(Exception):
                await client.unsubscribe(serial, queue)

    return StreamingResponse(iterator(), media_type="video/H264")


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8091")))


if __name__ == "__main__":
    main()
