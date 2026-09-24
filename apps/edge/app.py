"""HomeCam edge connector: the Home Assistant/Raspberry Pi (or adjacent
Docker/Compose host) side of Dahua "edge mode" (see
``apps/api/app/providers/dahua/edge_provider.py`` for the Azure-side
client and ``docs/edge-connector.md`` for the deployment guide).

Design goals (do not weaken without updating the docs and the Azure-side
client together):

- This process holds the real Dahua NVR username/password. Azure never
  receives them, in any response, ever.
- Azure authenticates to *this* process with a single bearer token
  (``HOME_CAM_EDGE_TOKEN``) that is completely unrelated to the Dahua
  credentials.
- This process is only reachable from Azure over a private overlay
  network (Tailscale recommended); it is never exposed on a public
  interface/port-forward.
- ``/channels/{n}/live`` never returns a raw ``rtsp://`` URL (let alone one
  with embedded credentials). It returns the HLS URL already being served
  by the MediaMTX relay (see ``docker-compose.yml`` in this directory),
  which pulls the credentialed RTSP feed on MediaMTX's own private
  network hop, not Azure's.

Intentionally self-contained (no imports from ``apps/api``): this process
ships and runs independently on the Pi/HA host.
"""
from __future__ import annotations

import asyncio
import os
import re
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
    # Base URL the browser/Azure should use for live streams, e.g. the
    # MediaMTX HLS endpoint reachable over the same private network as
    # this connector (typically http://<this-host-tailscale-addr>:8888).
    stream_base_url: str = "http://127.0.0.1:8888"
    timeout_seconds: float = 5.0

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
    )


@dataclass
class DahuaClient:
    """Thin, self-contained digest-auth CGI client for the Dahua NVR.

    Deliberately mirrors ``apps/api/app/providers/dahua/provider.py``'s CGI
    usage (same verified endpoints) but is copied rather than imported so
    this app has zero dependency on the ``apps/api`` package.
    """

    settings: EdgeSettings
    transport: httpx.AsyncBaseTransport | None = field(default=None)

    def _auth(self) -> httpx.DigestAuth:
        assert self.settings.dahua_username and self.settings.dahua_password
        return httpx.DigestAuth(self.settings.dahua_username, self.settings.dahua_password)

    async def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        async with httpx.AsyncClient(
            timeout=self.settings.timeout_seconds, transport=self.transport, follow_redirects=False
        ) as client:
            return await client.get(f"{self.settings.dahua_base_url}{path}", params=params, auth=self._auth())

    async def probe(self) -> tuple[bool, str]:
        if not self.settings.dahua_configured:
            return False, "DAHUA_HOST/DAHUA_USERNAME/DAHUA_PASSWORD not configured on the edge connector"
        try:
            response = await self._get("/cgi-bin/magicBox.cgi", {"action": "getSerialNo"})
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            return False, f"cannot reach Dahua NVR on the LAN: {exc}"
        if response.status_code in (401, 403):
            return False, "Dahua NVR rejected the configured credentials"
        if response.status_code >= 400:
            return False, f"Dahua NVR returned HTTP {response.status_code}"
        return True, "reachable"

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


def create_app(settings: EdgeSettings | None = None, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    settings = settings or settings_from_env()
    client = DahuaClient(settings, transport=transport)
    app = FastAPI(title="HomeCam edge connector", version="1.0.0")

    def require_token(authorization: str | None) -> None:
        if not settings.edge_token:
            # No token configured: refuse everything rather than silently
            # running unauthenticated, even on a private network.
            raise HTTPException(503, "Edge connector has no HOME_CAM_EDGE_TOKEN configured")
        expected = f"Bearer {settings.edge_token}"
        if authorization != expected:
            raise HTTPException(401, "Missing or invalid bearer token")

    @app.get("/healthz")
    async def healthz():
        """Unauthenticated liveness probe only (process is up); never
        reports Dahua reachability or any configuration detail."""
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
        return {
            "channels": [
                {"channel": c.channel, "name": c.name, "type": c.type, "online": dahua_reachable}
                for c in settings.channels
            ]
        }

    def _find_channel(channel: int) -> Channel:
        for c in settings.channels:
            if c.channel == channel:
                return c
        raise HTTPException(404, f"Unknown channel {channel}")

    @app.get("/channels/{channel}/snapshot")
    async def snapshot(channel: int, authorization: str | None = Header(default=None)):
        require_token(authorization)
        _find_channel(channel)
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
        _find_channel(channel)
        # Never returns Dahua's own rtsp:// URL/credentials: MediaMTX pulls
        # that feed on its own private-network hop (see docker-compose.yml)
        # and this only points at MediaMTX's browser-safe HLS output.
        return {"kind": "hls", "url": f"{settings.stream_base_url}/dahua-{channel}/index.m3u8"}

    return app


app = create_app()
