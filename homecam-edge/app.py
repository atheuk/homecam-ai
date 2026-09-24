"""Home Assistant Supervisor packaging of the HomeCam Dahua edge connector.

This file intentionally mirrors ``apps/edge/app.py``. The Home Assistant
Supervisor builds an add-on from this directory alone, so it cannot import
the standalone Compose application's source during the image build.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass, field

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import Response


@dataclass(frozen=True)
class Channel:
    channel: int
    name: str
    type: str = "camera"


@dataclass(frozen=True)
class EdgeSettings:
    dahua_scheme: str = "http"
    dahua_host: str | None = None
    dahua_port: int = 80
    dahua_username: str | None = None
    dahua_password: str | None = None
    dahua_channels: str = "1:Front Door"
    edge_token: str | None = None
    stream_base_url: str = "http://127.0.0.1:8888"
    timeout_seconds: float = 5.0
    channel_liveness_ttl_seconds: float = 60.0
    probe_ttl_seconds: float = 30.0
    # A failed reachability probe is cached far more briefly than a
    # successful one; see apps/edge/app.py for the full rationale.
    probe_failure_ttl_seconds: float = 5.0

    @property
    def dahua_configured(self) -> bool:
        return bool(self.dahua_host and self.dahua_username and self.dahua_password)

    @property
    def dahua_base_url(self) -> str:
        return f"{self.dahua_scheme}://{self.dahua_host}:{self.dahua_port}"

    @property
    def channels(self) -> list[Channel]:
        return parse_channels(self.dahua_channels)


def parse_channels(value: str) -> list[Channel]:
    channels: list[Channel] = []
    for raw in [part.strip() for part in value.split(",") if part.strip()]:
        match = re.fullmatch(r"(?P<channel>\d+)(?::(?P<name>[^:]+))?(?::(?P<type>camera|doorbell))?", raw)
        if not match:
            raise ValueError(f"Invalid DAHUA_CHANNELS entry: {raw!r}")
        channel = int(match.group("channel"))
        channels.append(
            Channel(
                channel=channel,
                name=(match.group("name") or f"Dahua channel {channel}").strip(),
                type=match.group("type") or "camera",
            )
        )
    return channels


def settings_from_env() -> EdgeSettings:
    return EdgeSettings(
        dahua_scheme=os.environ.get("DAHUA_SCHEME", "http"),
        dahua_host=os.environ.get("DAHUA_HOST") or None,
        dahua_port=int(os.environ.get("DAHUA_PORT", "80")),
        dahua_username=os.environ.get("DAHUA_USERNAME") or None,
        dahua_password=os.environ.get("DAHUA_PASSWORD") or None,
        dahua_channels=os.environ.get("DAHUA_CHANNELS", "1:Front Door"),
        edge_token=os.environ.get("HOME_CAM_EDGE_TOKEN") or None,
        stream_base_url=os.environ.get("STREAM_BASE_URL", "http://127.0.0.1:8888").rstrip("/"),
        timeout_seconds=float(os.environ.get("DAHUA_TIMEOUT_SECONDS", "5")),
        channel_liveness_ttl_seconds=float(os.environ.get("CHANNEL_LIVENESS_TTL_SECONDS", "60")),
        probe_ttl_seconds=float(os.environ.get("DAHUA_PROBE_TTL_SECONDS", "30")),
        probe_failure_ttl_seconds=float(os.environ.get("DAHUA_PROBE_FAILURE_TTL_SECONDS", "5")),
    )


@dataclass
class DahuaClient:
    settings: EdgeSettings
    transport: httpx.AsyncBaseTransport | None = field(default=None)
    # This NVR's embedded HTTP server only sustains ~1-2 concurrent CGI
    # sessions. Continuous AI-ingestion polling (Azure side) and per-channel
    # liveness probing both now add extra callers on top of real viewer
    # snapshot/stream requests, so every outbound call is serialized here
    # rather than relying on each caller to coordinate; overlapping requests
    # otherwise make the NVR reject *all* of them, including ones for
    # channels that are genuinely online.
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _probe_cache: tuple[bool, str, float] | None = field(default=None, init=False, repr=False)

    def _auth(self) -> httpx.DigestAuth:
        assert self.settings.dahua_username and self.settings.dahua_password
        return httpx.DigestAuth(self.settings.dahua_username, self.settings.dahua_password)

    async def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        async with self._lock:
            async with httpx.AsyncClient(
                timeout=self.settings.timeout_seconds, transport=self.transport, follow_redirects=False
            ) as client:
                return await client.get(f"{self.settings.dahua_base_url}{path}", params=params, auth=self._auth())

    def _ttl_for(self, reachable: bool) -> float:
        return self.settings.probe_ttl_seconds if reachable else self.settings.probe_failure_ttl_seconds

    async def probe(self) -> tuple[bool, str]:
        if not self.settings.dahua_configured:
            return False, "DAHUA_HOST/DAHUA_USERNAME/DAHUA_PASSWORD not configured on the edge connector"
        now = time.monotonic()
        cached = self._probe_cache
        if cached is not None and (now - cached[2]) < self._ttl_for(cached[0]):
            return cached[0], cached[1]
        try:
            response = await self._get("/cgi-bin/magicBox.cgi", {"action": "getSerialNo"})
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            result = (False, f"cannot reach Dahua NVR on the LAN: {exc}")
            self._probe_cache = (result[0], result[1], time.monotonic())
            return result
        if response.status_code in (401, 403):
            result = (False, "Dahua NVR rejected the configured credentials")
        elif response.status_code >= 400:
            result = (False, f"Dahua NVR returned HTTP {response.status_code}")
        else:
            result = (True, "reachable")
        self._probe_cache = (result[0], result[1], time.monotonic())
        return result

    async def snapshot(self, channel: int) -> bytes:
        # Cheap Dahua NVRs' embedded HTTP servers frequently reject a
        # snapshot.cgi request with a transient error if another request
        # (even a concurrent /health or /channels probe) is already in
        # flight. A short bounded retry absorbs that instead of surfacing
        # it to Azure as "camera unavailable".
        last_exc: Exception | None = None
        for attempt in range(3):
            if attempt:
                await asyncio.sleep(0.15 * attempt)
            try:
                response = await self._get("/cgi-bin/snapshot.cgi", {"channel": channel})
                response.raise_for_status()
                return response.content
            except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
        assert last_exc is not None
        raise last_exc


@dataclass
class ChannelLivenessTracker:
    """Real per-channel connectivity, cached with a TTL (mirrors
    ``apps/edge/app.py``; see that file for the full rationale)."""

    client: DahuaClient
    ttl_seconds: float = 60.0
    _cache: dict[int, tuple[bool, float]] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def is_online(self, channel: int) -> bool:
        cached = self.cached_online(channel)
        if cached is not None:
            return cached
        async with self._lock:
            cached = self.cached_online(channel)
            if cached is not None:
                return cached
            try:
                await self.client.snapshot(channel)
                online = True
            except Exception:  # noqa: BLE001 - any failure means "not live"
                online = False
            self._cache[channel] = (online, time.monotonic())
            return online

    def cached_online(self, channel: int) -> bool | None:
        """Last known liveness, or ``None`` when unknown/expired. Never calls out."""
        cached = self._cache.get(channel)
        if cached is None or (time.monotonic() - cached[1]) >= self.ttl_seconds:
            return None
        return cached[0]


def create_app(settings: EdgeSettings | None = None, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    settings = settings or settings_from_env()
    client = DahuaClient(settings, transport=transport)
    liveness = ChannelLivenessTracker(client=client, ttl_seconds=settings.channel_liveness_ttl_seconds)
    app = FastAPI(title="HomeCam edge connector", version="1.0.0")

    def require_token(authorization: str | None) -> None:
        if not settings.edge_token:
            raise HTTPException(503, "Edge connector has no HOME_CAM_EDGE_TOKEN configured")
        if authorization != f"Bearer {settings.edge_token}":
            raise HTTPException(401, "Missing or invalid bearer token")

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/health")
    async def health(authorization: str | None = Header(default=None)):
        require_token(authorization)
        dahua_reachable, message = await client.probe()
        return {"dahua_reachable": dahua_reachable, "message": message}

    @app.get("/channels")
    async def channels(authorization: str | None = Header(default=None)):
        require_token(authorization)
        dahua_reachable, _ = await client.probe()
        statuses = []
        for c in settings.channels:
            if dahua_reachable:
                statuses.append(await liveness.is_online(c.channel))
            else:
                # A failed reachability probe is not proof a camera went
                # away; keep trusting a recent successful snapshot rather
                # than blanking every camera out of the web app.
                statuses.append(liveness.cached_online(c.channel) is True)
        return {
            "channels": [
                {"channel": c.channel, "name": c.name, "type": c.type, "online": online}
                for c, online in zip(settings.channels, statuses)
            ]
        }

    def find_channel(channel: int) -> Channel:
        for configured in settings.channels:
            if configured.channel == channel:
                return configured
        raise HTTPException(404, f"Unknown channel {channel}")

    @app.get("/channels/{channel}/snapshot")
    async def snapshot(channel: int, authorization: str | None = Header(default=None)):
        require_token(authorization)
        find_channel(channel)
        try:
            content = await client.snapshot(channel)
        except httpx.HTTPStatusError as exc:
            raise HTTPException(503, f"Dahua snapshot failed: {exc}") from exc
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise HTTPException(503, f"Dahua NVR unreachable: {exc}") from exc
        return Response(content, media_type="image/jpeg")

    @app.get("/channels/{channel}/live")
    async def live(channel: int, authorization: str | None = Header(default=None)):
        require_token(authorization)
        find_channel(channel)
        return {"kind": "hls", "url": f"{settings.stream_base_url}/dahua-{channel}/index.m3u8"}

    return app


app = create_app()
