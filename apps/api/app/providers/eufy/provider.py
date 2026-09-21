from __future__ import annotations

import asyncio
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


@dataclass(frozen=True)
class EufySettings:
    adapter_url: str | None = None
    adapter_token: str | None = None
    timeout_seconds: float = 10.0
    retries: int = 1

    @property
    def configured(self) -> bool:
        return bool(self.adapter_url)


class EufyEdgeProvider:
    """Provider boundary for a local Eufy bridge process.

    HomeCam intentionally does not implement cloud protocol details. A local
    adapter owns Eufy sessions, 2FA/captcha/re-auth flows, and maps available
    device facts to this small HTTP contract.
    """

    id = "eufy"

    def __init__(
        self,
        settings: EufySettings,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.settings = settings
        self._transport = transport
        self._devices: dict[str, dict] = {}

    async def get_provider_info(self) -> ProviderInfo:
        return {"id": self.id, "name": "Eufy Edge Adapter", "manufacturer": "Eufy"}

    def _headers(self) -> dict[str, str]:
        if not self.settings.adapter_token:
            return {}
        return {"Authorization": f"Bearer {self.settings.adapter_token}"}

    async def _request(self, operation: str, path: str) -> httpx.Response:
        if not self.settings.adapter_url:
            raise ProviderUnavailableError("Eufy adapter URL is not configured")
        url = f"{self.settings.adapter_url.rstrip('/')}{path}"
        last_error: Exception | None = None
        for attempt in range(self.settings.retries + 1):
            try:
                async with httpx.AsyncClient(
                    timeout=self.settings.timeout_seconds,
                    transport=self._transport,
                    follow_redirects=False,
                ) as client:
                    response = await client.get(url, headers=self._headers())
                if response.status_code in (401, 403):
                    raise ProviderRequestError(self.id, operation, "adapter authentication rejected", retryable=False)
                if response.status_code == 409:
                    raise ProviderRequestError(self.id, operation, "adapter requires user re-authentication", retryable=False)
                if response.status_code >= 500:
                    raise ProviderRequestError(
                        self.id, operation, f"adapter returned HTTP {response.status_code}", retryable=True
                    )
                if response.status_code >= 400:
                    raise ProviderRequestError(
                        self.id, operation, f"adapter returned HTTP {response.status_code}", retryable=False
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
        message = str(last_error) if last_error else "unknown adapter error"
        raise ProviderUnavailableError(f"Eufy {operation} unavailable: {message}")

    @staticmethod
    def _capabilities(raw: dict) -> dict[str, str]:
        raw_caps = raw.get("capabilities", {})
        return {
            "snapshot": SUPPORTED if raw_caps.get("snapshot") else UNAVAILABLE,
            "liveStream": SUPPORTED if raw_caps.get("liveStream") else UNAVAILABLE,
            "recordings": UNAVAILABLE,
            "motionEvents": SUPPORTED if raw_caps.get("motionEvents") else UNKNOWN,
            "personEvents": SUPPORTED if raw_caps.get("personEvents") else UNKNOWN,
            "vehicleEvents": UNSUPPORTED,
            "doorbellEvents": SUPPORTED if raw_caps.get("doorbellEvents") else UNKNOWN,
            "twoWayAudio": UNAVAILABLE,
            "battery": SUPPORTED if raw_caps.get("battery") else UNKNOWN,
            "ptz": UNSUPPORTED,
            "eventImages": SUPPORTED if raw_caps.get("eventImages") else UNKNOWN,
            # The edge adapter does not expose a normalized audio buffer yet;
            # HomeCam never fabricates audio, so this stays UNAVAILABLE.
            AUDIO_DETECTION: audio_detection_status(bool(raw_caps.get("audioBuffer")), False),
        }

    @classmethod
    def _camera_record(cls, raw: dict) -> dict:
        device_id = str(raw["id"])
        status = raw.get("status") or (CameraStatus.ONLINE.value if raw.get("online", True) else CameraStatus.OFFLINE.value)
        return {
            "id": f"eufy-{device_id}",
            "provider_id": cls.id,
            "name": str(raw.get("name") or f"Eufy device {device_id}"),
            "type": str(raw.get("type") or "doorbell"),
            "model": str(raw.get("model") or "Eufy device"),
            "online": status == CameraStatus.ONLINE.value,
            "status": status,
            "battery_level": raw.get("battery_level"),
            "capabilities": cls._capabilities(raw),
            "adapter_device_id": device_id,
        }

    async def _refresh_devices(self) -> None:
        if not self.settings.configured:
            self._devices = {}
            return
        response = await self._request("device discovery", "/devices")
        payload = response.json()
        devices = payload["devices"] if isinstance(payload, dict) and "devices" in payload else payload
        self._devices = {record["id"]: record for record in [self._camera_record(device) for device in devices]}

    def has_camera(self, camera_id: str) -> bool:
        return camera_id.startswith("eufy-")

    async def discover_devices(self) -> list[dict]:
        if not self.settings.configured:
            return []
        await self._refresh_devices()
        return [dict(device) for device in self._devices.values()]

    async def _device(self, camera_id: str) -> dict:
        if camera_id not in self._devices:
            await self._refresh_devices()
        device = self._devices.get(camera_id)
        if not device:
            raise CameraNotFoundError(camera_id)
        return device

    async def get_capabilities(self, camera_id: str) -> dict[str, str]:
        return dict((await self._device(camera_id))["capabilities"])

    async def get_snapshot(self, camera_id: str) -> bytes:
        device = await self._device(camera_id)
        if device["status"] != CameraStatus.ONLINE.value:
            raise CameraOfflineError(camera_id)
        response = await self._request("snapshot", f"/devices/{device['adapter_device_id']}/snapshot")
        return response.content

    async def get_live_stream(self, camera_id: str) -> str:
        device = await self._device(camera_id)
        if device["status"] != CameraStatus.ONLINE.value:
            raise CameraOfflineError(camera_id)
        response = await self._request("live stream descriptor", f"/devices/{device['adapter_device_id']}/live")
        payload = response.json()
        url = payload.get("hls_url") or payload.get("rtsp_url") or payload.get("url")
        if not url:
            raise ProviderUnavailableError("Eufy adapter did not return a stream URL")
        return str(url)

    async def get_health(self) -> ProviderHealth:
        if not self.settings.configured:
            return {
                "provider_id": self.id,
                "status": ProviderStatus.DEGRADED.value,
                "message": "Eufy provider is enabled but EUFY_ADAPTER_URL is not configured.",
                "camera_count": 0,
                "online_camera_count": 0,
            }
        try:
            health_response = await self._request("health", "/health")
            payload = health_response.json()
            await self._refresh_devices()
        except ProviderUnavailableError as exc:
            return {
                "provider_id": self.id,
                "status": ProviderStatus.OFFLINE.value,
                "message": str(exc),
                "camera_count": len(self._devices),
                "online_camera_count": 0,
            }
        auth_state = str(payload.get("auth_state", "unknown"))
        camera_count = len(self._devices)
        online_count = sum(1 for device in self._devices.values() if device["online"])
        if auth_state in {"authenticated", "ready"}:
            status = ProviderStatus.ONLINE.value if online_count == camera_count else ProviderStatus.DEGRADED.value
            message = "Eufy adapter authenticated."
        elif auth_state in {"2fa_required", "captcha_required", "reauth_required", "unauthenticated"}:
            status = ProviderStatus.DEGRADED.value
            message = f"Eufy adapter requires local user action: {auth_state}."
        else:
            status = ProviderStatus.UNKNOWN.value
            message = f"Eufy adapter auth state is {auth_state}."
        return {
            "provider_id": self.id,
            "status": status,
            "message": message,
            "camera_count": camera_count,
            "online_camera_count": online_count,
        }
