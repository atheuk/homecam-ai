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
import logging
import os
import secrets
from dataclasses import dataclass
from urllib.parse import quote

from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse, Response, StreamingResponse

from eufy_ws import EufyBridgeError, EufyBridgeUnavailable, EufyWsClient

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("eufy-edge")

JPEG_START_OF_IMAGE = b"\xff\xd8"


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
    return {"hls_url": f"{settings.go2rtc_public_url}/api/stream.m3u8?src={quote(name)}"}


@app.get("/internal/devices/{device_id}/h264")
async def internal_h264(device_id: str, token: str = Query(default="")) -> StreamingResponse:
    """Raw H.264 Annex-B byte stream for go2rtc's ffmpeg source.

    Authenticated with a separate single-purpose token passed in the query
    string, because ffmpeg sources cannot easily set request headers. This
    endpoint is never exposed to HomeCam or the browser.
    """
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


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8091")))


if __name__ == "__main__":
    main()
