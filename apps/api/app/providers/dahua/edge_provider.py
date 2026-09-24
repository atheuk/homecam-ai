"""Dahua "edge connector" mode (Home Assistant / Raspberry Pi bridge).

Some users cannot (and should not) forward the Dahua NVR's HTTP/RTSP ports
to the public internet. Instead, they run a small edge connector process on
their existing Home Assistant/Raspberry Pi host (or an adjacent Docker/
Compose host on the same LAN) that:

- talks to the Dahua NVR directly on the LAN using the same verified
  digest-auth CGI surface as :mod:`app.providers.dahua.provider`;
- is reachable from Azure only over a private overlay network (Tailscale is
  recommended, see ``docs/edge-connector.md``);
- exposes a small token-authenticated HTTP contract instead of raw Dahua
  credentials or raw credentialed RTSP.

This module is the Azure-side client for that contract. It intentionally
never sees the Dahua username/password: only the edge connector's base URL
and its own bearer token, which are unrelated to the NVR's own credentials.

Edge connector HTTP contract (also implemented by ``apps/edge``):

- ``GET /health``    -> ``{"dahua_reachable": bool, "message": str}``
- ``GET /channels``  -> ``{"channels": [{"channel": int, "name": str, "type": "camera"|"doorbell", "online": bool}]}``
- ``GET /channels/{channel}/snapshot`` -> ``image/jpeg`` bytes
- ``GET /channels/{channel}/live``     -> ``{"kind": "hls"|"webrtc"|"rtsp", "url": str}``

Every request (except the connector's own unauthenticated ``/healthz``
liveness probe) must carry ``Authorization: Bearer <HOME_CAM_EDGE_TOKEN>``.
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass

import httpx

from ..base import (
    CameraNotFoundError,
    CameraOfflineError,
    CameraStatus,
    CapabilityStatus,
    ProviderHealth,
    ProviderInfo,
    ProviderRequestError,
    ProviderStatus,
    ProviderUnavailableError,
)
from ..capabilities import AUDIO_DETECTION, audio_detection_status

SUPPORTED = CapabilityStatus.SUPPORTED.value
UNSUPPORTED = CapabilityStatus.UNSUPPORTED.value
UNKNOWN = CapabilityStatus.UNKNOWN.value
UNAVAILABLE = CapabilityStatus.UNAVAILABLE.value

# How long a channel the edge connector previously reported online keeps
# that status while the connector reports it offline. See _refresh_channels.
EDGE_OFFLINE_GRACE_SECONDS = 180.0


@dataclass(frozen=True)
class DahuaEdgeSettings:
    """Connection settings for the Azure -> edge connector hop only.

    ``base_url`` should be the edge connector's private-network address
    (e.g. its Tailscale IP/MagicDNS name), never a publicly exposed one.
    ``token`` is the edge connector's own bearer token
    (``HOME_CAM_EDGE_TOKEN``); it is unrelated to the Dahua NVR username or
    password, neither of which Azure ever holds in this mode.
    """

    base_url: str | None = None
    token: str | None = None
    timeout_seconds: float = 5.0
    retries: int = 1
    proxy_url: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.base_url)


class DahuaEdgeProvider:
    """Provider boundary for a local Dahua edge connector.

    Keeps the same ``id`` and ``dahua-channel-<n>`` camera id scheme as the
    direct-mode :class:`app.providers.dahua.provider.DahuaProvider` so
    switching a saved configuration between "direct" and "edge" mode is
    transparent to cameras, events, and zones already stored for this NVR.
    """

    id = "dahua"

    def __init__(
        self,
        settings: DahuaEdgeSettings,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.settings = settings
        self._transport = transport
        self._channels: dict[str, dict] = {}
        # camera_id -> monotonic timestamp of the last time the edge
        # connector reported this channel online. See _refresh_channels.
        self._last_online_at: dict[str, float] = {}

    async def get_provider_info(self) -> ProviderInfo:
        return {"id": self.id, "name": "Dahua NVR (edge connector)", "manufacturer": "Dahua"}

    def _headers(self) -> dict[str, str]:
        if not self.settings.token:
            return {}
        return {"Authorization": f"Bearer {self.settings.token}"}

    async def _request(self, operation: str, path: str) -> httpx.Response:
        if not self.settings.base_url:
            raise ProviderUnavailableError(
                "Dahua edge mode is enabled but no edge connector base URL is configured"
            )
        url = f"{self.settings.base_url.rstrip('/')}{path}"
        last_error: Exception | None = None
        for attempt in range(self.settings.retries + 1):
            try:
                client_options: dict = {
                    "timeout": self.settings.timeout_seconds,
                    "follow_redirects": False,
                }
                if self._transport is not None:
                    client_options["transport"] = self._transport
                else:
                    proxy_url = self.settings.proxy_url or os.environ.get("TAILSCALE_HTTP_PROXY")
                    if proxy_url:
                        client_options["proxy"] = proxy_url
                async with httpx.AsyncClient(**client_options) as client:
                    response = await client.get(url, headers=self._headers())
                if response.status_code in (401, 403):
                    raise ProviderRequestError(self.id, operation, "edge connector authentication rejected", retryable=False)
                if response.status_code >= 500:
                    raise ProviderRequestError(
                        self.id, operation, f"edge connector returned HTTP {response.status_code}", retryable=True
                    )
                if response.status_code >= 400:
                    raise ProviderRequestError(
                        self.id, operation, f"edge connector returned HTTP {response.status_code}", retryable=False
                    )
                return response
            except ProviderRequestError as exc:
                last_error = exc
                if not exc.retryable or attempt >= self.settings.retries:
                    break
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                if attempt >= self.settings.retries:
                    break
            await asyncio.sleep(0.1 * (attempt + 1))
        message = str(last_error) if last_error else "unknown edge connector error"
        raise ProviderUnavailableError(f"Dahua edge {operation} unavailable: {message}")

    @staticmethod
    def _capabilities(info: dict) -> dict[str, str]:
        configured_status = SUPPORTED if info.get("online", True) else UNAVAILABLE
        return {
            "snapshot": configured_status,
            "liveStream": configured_status,
            "recordings": UNAVAILABLE,
            "motionEvents": UNKNOWN,
            "personEvents": UNKNOWN,
            "vehicleEvents": UNKNOWN,
            "doorbellEvents": UNSUPPORTED if info.get("type") != "doorbell" else UNKNOWN,
            "twoWayAudio": UNKNOWN,
            "battery": UNSUPPORTED,
            "ptz": UNKNOWN,
            "storageHealth": UNAVAILABLE,
            # The edge connector contract does not expose a normalized audio
            # buffer; HomeCam never fabricates audio data.
            AUDIO_DETECTION: audio_detection_status(False, False),
        }

    async def _refresh_channels(self) -> None:
        if not self.settings.configured:
            self._channels = {}
            return
        response = await self._request("channel discovery", "/channels")
        payload = response.json()
        raw_channels = payload["channels"] if isinstance(payload, dict) and "channels" in payload else payload
        channels: dict[str, dict] = {}
        for raw in raw_channels:
            channel_number = int(raw["channel"])
            camera_id = f"dahua-channel-{channel_number}"
            reported_online = bool(raw.get("online", True))
            # The NVR behind the edge connector only sustains ~1-2 concurrent
            # CGI sessions and refuses one whenever it is busy, which the
            # connector cannot always distinguish from a camera going away.
            # Measured against the deployed system, channels that were
            # streaming fine flipped to offline and back repeatedly, which
            # hid working cameras from the web app. A channel the connector
            # confirmed online within the grace window is therefore still
            # reported online; once the window lapses without a single
            # confirmation it does go offline. Channels that have never been
            # seen online (no camera attached) are unaffected.
            #
            # Newer edge connectors apply their own hysteresis, so in
            # practice this only matters until the Home Assistant add-on is
            # updated -- but it must stay correct either way.
            if reported_online:
                self._last_online_at[camera_id] = time.monotonic()
                online = True
            else:
                last_seen = self._last_online_at.get(camera_id)
                online = last_seen is not None and (time.monotonic() - last_seen) < EDGE_OFFLINE_GRACE_SECONDS
            channels[camera_id] = {
                "channel": channel_number,
                "name": str(raw.get("name") or f"Dahua channel {channel_number}"),
                "type": str(raw.get("type") or "camera"),
                "online": online,
            }
        self._channels = channels

    def has_camera(self, camera_id: str) -> bool:
        return camera_id in self._channels or camera_id.startswith("dahua-channel-")

    async def _channel(self, camera_id: str) -> dict:
        if camera_id not in self._channels:
            await self._refresh_channels()
        info = self._channels.get(camera_id)
        if info is None:
            raise CameraNotFoundError(camera_id)
        return info

    async def discover_devices(self) -> list[dict]:
        if not self.settings.configured:
            return []
        await self._refresh_channels()
        return [
            {
                "id": camera_id,
                "provider_id": self.id,
                "name": info["name"],
                "type": info["type"],
                "model": "DHI-NVR4204-P-4KS2 (via edge connector)",
                "online": info["online"],
                "status": CameraStatus.ONLINE.value if info["online"] else CameraStatus.OFFLINE.value,
                "battery_level": None,
                "capabilities": self._capabilities(info),
            }
            for camera_id, info in self._channels.items()
        ]

    async def get_capabilities(self, camera_id: str) -> dict[str, str]:
        return self._capabilities(await self._channel(camera_id))

    async def get_snapshot(self, camera_id: str) -> bytes:
        info = await self._channel(camera_id)
        if not info.get("online", True):
            raise CameraOfflineError(camera_id)
        response = await self._request("snapshot", f"/channels/{info['channel']}/snapshot")
        if not response.content:
            raise CameraOfflineError(camera_id)
        return response.content

    async def get_live_stream(self, camera_id: str) -> str:
        """Return a browser-safe stream descriptor URL.

        The edge connector is responsible for translating the credentialed
        Dahua RTSP feed into an HLS/WebRTC URL (typically via a MediaMTX
        relay, see ``docs/edge-connector.md``); Azure/HomeCam never sees the
        raw Dahua RTSP URL or credentials in this mode.
        """
        info = await self._channel(camera_id)
        if not info.get("online", True):
            raise CameraOfflineError(camera_id)
        response = await self._request("live stream descriptor", f"/channels/{info['channel']}/live")
        payload = response.json()
        url = payload.get("url")
        if not url:
            raise ProviderUnavailableError("Dahua edge connector did not return a stream URL")
        return str(url)

    async def get_health(self) -> ProviderHealth:
        if not self.settings.configured:
            return {
                "provider_id": self.id,
                "status": ProviderStatus.DEGRADED.value,
                "message": (
                    "Dahua edge mode is enabled but no edge connector base URL is configured. "
                    "QR/DMSS P2P serial pairing alone does not give HomeCam network access: "
                    "deploy the edge connector on Home Assistant/Raspberry Pi and enter its "
                    "private (e.g. Tailscale) base URL and token."
                ),
                "camera_count": 0,
                "online_camera_count": 0,
            }
        try:
            health_response = await self._request("edge health", "/health")
            payload = health_response.json()
            await self._refresh_channels()
        except ProviderUnavailableError as exc:
            return {
                "provider_id": self.id,
                "status": ProviderStatus.OFFLINE.value,
                "message": str(exc),
                "camera_count": len(self._channels),
                "online_camera_count": 0,
            }
        dahua_reachable = bool(payload.get("dahua_reachable", False))
        camera_count = len(self._channels)
        online_count = sum(1 for info in self._channels.values() if info.get("online"))
        if not camera_count:
            return {
                "provider_id": self.id,
                "status": ProviderStatus.DEGRADED.value,
                "message": "Edge connector reachable, but it reported no Dahua channels yet.",
                "camera_count": 0,
                "online_camera_count": 0,
            }
        if not dahua_reachable:
            return {
                "provider_id": self.id,
                "status": ProviderStatus.DEGRADED.value,
                "message": (
                    "Edge connector reachable over the private network, but it cannot reach the "
                    f"Dahua NVR on its LAN: {payload.get('message', 'unknown error')}"
                ),
                "camera_count": camera_count,
                "online_camera_count": online_count,
            }
        status = ProviderStatus.ONLINE.value if online_count == camera_count else ProviderStatus.DEGRADED.value
        return {
            "provider_id": self.id,
            "status": status,
            "message": "Edge connector reachable over the private network; Dahua NVR reachable on the LAN.",
            "camera_count": camera_count,
            "online_camera_count": online_count,
        }
