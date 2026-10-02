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

import contextlib
import ipaddress
import logging
import os
import re
import secrets
from dataclasses import dataclass
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
        )


settings = Settings.from_env()
client = EufyWsClient(settings.eufy_ws_url, live_idle_stop_seconds=settings.live_idle_stop_seconds)


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
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
    return {"devices": client.devices()}


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
