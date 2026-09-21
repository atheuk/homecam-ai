from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

import httpx

from ..base import (
    CameraNotFoundError,
    CameraOfflineError,
    CameraStatus,
    CapabilityStatus,
    ProviderConfigurationError,
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


@dataclass(frozen=True)
class DahuaChannel:
    channel: int
    name: str
    camera_type: str = "camera"

    @property
    def camera_id(self) -> str:
        return f"dahua-channel-{self.channel}"


@dataclass(frozen=True)
class DahuaSettings:
    scheme: str = "http"
    host: str | None = None
    port: int = 80
    username: str | None = None
    password: str | None = None
    serial: str = "5J006FCPAZ6B52A"
    channels: str = ""
    timeout_seconds: float = 5.0
    retries: int = 1

    @property
    def configured(self) -> bool:
        return bool(self.host and self.username and self.password)

    @property
    def base_url(self) -> str:
        if not self.host:
            raise ProviderConfigurationError("DAHUA_HOST is required")
        scheme = "https" if self.scheme == "https" else "http"
        return f"{scheme}://{self.host}:{self.port}"


def parse_channels(value: str) -> list[DahuaChannel]:
    channels: list[DahuaChannel] = []
    for raw in [part.strip() for part in value.split(",") if part.strip()]:
        match = re.fullmatch(r"(?P<channel>\d+)(?::(?P<name>[^:]+))?(?::(?P<type>camera|doorbell))?", raw)
        if not match:
            raise ProviderConfigurationError(
                "DAHUA_CHANNELS must use entries like '1:Front Door' or '2:Driveway:camera'"
            )
        channel = int(match.group("channel"))
        channels.append(
            DahuaChannel(
                channel=channel,
                name=(match.group("name") or f"Dahua channel {channel}").strip(),
                camera_type=match.group("type") or "camera",
            )
        )
    return channels


class DahuaProvider:
    id = "dahua"

    def __init__(
        self,
        settings: DahuaSettings,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.settings = settings
        self._transport = transport
        self._channels = parse_channels(settings.channels)

    async def get_provider_info(self) -> ProviderInfo:
        return {"id": self.id, "name": "Dahua NVR", "manufacturer": "Dahua"}

    def has_camera(self, camera_id: str) -> bool:
        return any(channel.camera_id == camera_id for channel in self._channels)

    def _channel_for_camera(self, camera_id: str) -> DahuaChannel:
        for channel in self._channels:
            if channel.camera_id == camera_id:
                return channel
        raise CameraNotFoundError(camera_id)

    def _auth(self) -> httpx.DigestAuth:
        if not self.settings.username or not self.settings.password:
            raise ProviderConfigurationError("DAHUA_USERNAME and DAHUA_PASSWORD are required")
        return httpx.DigestAuth(self.settings.username, self.settings.password)

    async def _request(self, operation: str, path: str, params: dict[str, str | int] | None = None) -> httpx.Response:
        if not self.settings.configured:
            raise ProviderUnavailableError(
                "Dahua provider is enabled but DAHUA_HOST, DAHUA_USERNAME, or DAHUA_PASSWORD is missing"
            )
        url = f"{self.settings.base_url}{path}"
        last_error: Exception | None = None
        for attempt in range(self.settings.retries + 1):
            try:
                async with httpx.AsyncClient(
                    timeout=self.settings.timeout_seconds,
                    transport=self._transport,
                    follow_redirects=False,
                ) as client:
                    response = await client.get(url, params=params, auth=self._auth())
                if response.status_code in (401, 403):
                    raise ProviderRequestError(self.id, operation, "authentication rejected", retryable=False)
                if response.status_code >= 500:
                    raise ProviderRequestError(
                        self.id, operation, f"upstream returned HTTP {response.status_code}", retryable=True
                    )
                if response.status_code >= 400:
                    raise ProviderRequestError(
                        self.id, operation, f"upstream returned HTTP {response.status_code}", retryable=False
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
        message = str(last_error) if last_error else "unknown upstream error"
        raise ProviderUnavailableError(f"Dahua {operation} unavailable: {message}")

    async def _probe_serial(self) -> str | None:
        response = await self._request("serial probe", "/cgi-bin/magicBox.cgi", {"action": "getSerialNo"})
        text = response.text.strip()
        if "sn=" in text:
            return text.split("sn=", 1)[1].splitlines()[0].strip()
        if "serialNumber=" in text:
            return text.split("serialNumber=", 1)[1].splitlines()[0].strip()
        return text or None

    async def discover_devices(self) -> list[dict]:
        if not self.settings.configured or not self._channels:
            return []
        return [
            {
                "id": channel.camera_id,
                "provider_id": self.id,
                "name": channel.name,
                "type": channel.camera_type,
                "model": "DHI-NVR4204-P-4KS2",
                "online": True,
                "status": CameraStatus.ONLINE.value,
                "battery_level": None,
                "capabilities": await self.get_capabilities(channel.camera_id),
            }
            for channel in self._channels
        ]

    async def get_capabilities(self, camera_id: str) -> dict[str, str]:
        self._channel_for_camera(camera_id)
        configured_status = SUPPORTED if self.settings.configured else UNAVAILABLE
        return {
            "snapshot": configured_status,
            "liveStream": configured_status,
            "recordings": configured_status,
            "motionEvents": configured_status,
            "personEvents": UNKNOWN,
            "vehicleEvents": UNKNOWN,
            "doorbellEvents": UNSUPPORTED,
            "twoWayAudio": UNKNOWN,
            "battery": UNSUPPORTED,
            "ptz": UNKNOWN,
            "storageHealth": configured_status,
            # No normalized audio-buffer API is exposed by this adapter yet,
            # so the speech-like-activity stage has nothing to consume.
            AUDIO_DETECTION: audio_detection_status(False, False),
        }

    async def get_snapshot(self, camera_id: str) -> bytes:
        channel = self._channel_for_camera(camera_id)
        response = await self._request("snapshot", "/cgi-bin/snapshot.cgi", {"channel": channel.channel})
        if not response.content:
            raise CameraOfflineError(camera_id)
        return response.content

    async def get_live_stream(self, camera_id: str) -> str:
        channel = self._channel_for_camera(camera_id)
        if not self.settings.host:
            raise ProviderUnavailableError("Dahua host is not configured")
        return f"rtsp://{self.settings.host}:554/cam/realmonitor?channel={channel.channel}&subtype=0"

    async def poll_events(self, camera_id: str, event_codes: list[str] | None = None) -> str:
        channel = self._channel_for_camera(camera_id)
        codes = event_codes or ["VideoMotion"]
        response = await self._request(
            "event subscription",
            "/cgi-bin/eventManager.cgi",
            {"action": "attach", "codes": "[" + ",".join(codes) + "]", "channel": channel.channel},
        )
        return response.text

    async def search_recordings(self, camera_id: str, start_time: str, end_time: str) -> str:
        channel = self._channel_for_camera(camera_id)
        response = await self._request(
            "recording search",
            "/cgi-bin/mediaFileFind.cgi",
            {"action": "findFile", "object": "homecam", "channel": channel.channel, "startTime": start_time, "endTime": end_time},
        )
        return response.text

    async def get_storage_health(self) -> str:
        response = await self._request("storage health", "/cgi-bin/storage.cgi", {"action": "getDeviceAllInfo"})
        return response.text

    async def get_health(self) -> ProviderHealth:
        if not self.settings.configured:
            return {
                "provider_id": self.id,
                "status": ProviderStatus.DEGRADED.value,
                "message": "Dahua enabled but LAN host and/or local credentials are not configured.",
                "camera_count": len(self._channels),
                "online_camera_count": 0,
            }
        if not self._channels:
            return {
                "provider_id": self.id,
                "status": ProviderStatus.DEGRADED.value,
                "message": "Dahua host is configured; set DAHUA_CHANNELS to expose NVR channels.",
                "camera_count": 0,
                "online_camera_count": 0,
            }
        try:
            serial = await self._probe_serial()
        except ProviderUnavailableError as exc:
            return {
                "provider_id": self.id,
                "status": ProviderStatus.OFFLINE.value,
                "message": str(exc),
                "camera_count": len(self._channels),
                "online_camera_count": 0,
            }
        serial_note = f" serial {serial}" if serial else ""
        if serial and self.settings.serial and serial != self.settings.serial:
            return {
                "provider_id": self.id,
                "status": ProviderStatus.DEGRADED.value,
                "message": f"Dahua reachable, but serial {serial} does not match expected {self.settings.serial}.",
                "camera_count": len(self._channels),
                "online_camera_count": len(self._channels),
            }
        return {
            "provider_id": self.id,
            "status": ProviderStatus.ONLINE.value,
            "message": f"Dahua NVR reachable{serial_note}.",
            "camera_count": len(self._channels),
            "online_camera_count": len(self._channels),
        }
