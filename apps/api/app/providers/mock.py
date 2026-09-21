"""Deterministic mock camera provider (SPEC section 40).

Two provider instances are created from this single implementation:

- ``mock_provider`` (id "mock"): Front Door, Driveway, Backyard, Garden.
- ``mock_eufy_provider`` (id "mock-eufy"): Front Doorbell, simulated as a
  battery-powered Eufy T8210 behind a HomeBase 2.

Each provider tracks its own camera state independently so a simulated
outage of one provider (e.g. the Eufy HomeBase going offline) never affects
cameras served by the other provider (SPEC 2.5 / 43).
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from .base import (
    CameraNotFoundError,
    CameraOfflineError,
    CameraStatus,
    CapabilityStatus,
    ProviderHealth,
    ProviderInfo,
    ProviderStatus,
    ProviderUnavailableError,
)

SUPPORTED = CapabilityStatus.SUPPORTED.value
UNSUPPORTED = CapabilityStatus.UNSUPPORTED.value
UNKNOWN = CapabilityStatus.UNKNOWN.value


class MockCameraProvider:
    """A single mock provider instance managing one or more mock cameras."""

    def __init__(self, provider_id: str, name: str, manufacturer: str, cameras: list[dict]):
        self.id = provider_id
        self.name = name
        self.manufacturer = manufacturer
        # Keyed by camera id -> mutable state dict; capabilities is the
        # SPEC-5 capability-status map, not a flat feature list.
        self._cameras: dict[str, dict] = {c["id"]: {**c, "provider_id": provider_id} for c in cameras}
        self._unavailable = False

    async def get_provider_info(self) -> ProviderInfo:
        return {"id": self.id, "name": self.name, "manufacturer": self.manufacturer}

    def _require_available(self) -> None:
        if self._unavailable:
            raise ProviderUnavailableError(f"provider '{self.id}' is currently unavailable")

    def _require_camera(self, camera_id: str) -> dict:
        camera = self._cameras.get(camera_id)
        if camera is None:
            raise CameraNotFoundError(camera_id)
        return camera

    async def discover_devices(self) -> list[dict]:
        self._require_available()
        return [dict(c) for c in self._cameras.values()]

    async def get_capabilities(self, camera_id: str) -> dict[str, str]:
        self._require_available()
        return dict(self._require_camera(camera_id)["capabilities"])

    async def get_snapshot(self, camera_id: str) -> bytes:
        self._require_available()
        camera = self._require_camera(camera_id)
        if camera["status"] != CameraStatus.ONLINE.value:
            raise CameraOfflineError(camera_id)
        # Deterministic placeholder; a real media pipeline can replace this
        # without changing the provider contract or the API shape.
        return f"HOME_CAM_MOCK_SNAPSHOT:{camera_id}".encode()

    async def get_live_stream(self, camera_id: str) -> str:
        self._require_available()
        camera = self._require_camera(camera_id)
        if camera["status"] != CameraStatus.ONLINE.value:
            raise CameraOfflineError(camera_id)
        return f"http://localhost:8889/{camera_id}/index.m3u8"

    async def get_health(self) -> ProviderHealth:
        cameras = list(self._cameras.values())
        online_count = sum(1 for c in cameras if c["status"] == CameraStatus.ONLINE.value)
        if self._unavailable:
            status, message = ProviderStatus.OFFLINE.value, f"{self.name} connection unavailable."
        elif online_count == 0:
            status, message = ProviderStatus.OFFLINE.value, "All cameras are offline."
        elif online_count < len(cameras):
            status, message = ProviderStatus.DEGRADED.value, f"{len(cameras) - online_count} camera(s) offline."
        else:
            status, message = ProviderStatus.ONLINE.value, "All cameras reporting normally."
        return {
            "provider_id": self.id,
            "status": status,
            "message": message,
            "camera_count": len(cameras),
            "online_camera_count": online_count,
        }

    def event(self, camera_id: str, type: str) -> dict:
        camera = self._require_camera(camera_id)
        now = datetime.now(timezone.utc)
        priority = "high" if type in ("doorbell", "battery_low", "intrusion") else "normal"
        event_id = "evt-" + hashlib.sha1(
            f"{camera_id}:{type}:{now.isoformat()}".encode()
        ).hexdigest()[:16]
        return {
            "id": event_id,
            "camera_id": camera_id,
            "type": type,
            "priority": priority,
            "source": "provider",
            "start_time": now.isoformat(),
            "description": f"Mock {type.replace('_', ' ')} detected on {camera['name']}",
        }

    def simulate_status(self, camera_id: str, status: str) -> dict:
        """Set a camera's status to online/offline/degraded/unknown."""
        camera = self._require_camera(camera_id)
        camera["status"] = status
        camera["online"] = status == CameraStatus.ONLINE.value
        return dict(camera)

    def simulate_battery(self, camera_id: str, battery_level: int) -> dict:
        camera = self._require_camera(camera_id)
        if camera["battery_level"] is None:
            raise ValueError(f"camera '{camera_id}' does not support battery reporting")
        camera["battery_level"] = max(0, min(100, battery_level))
        return dict(camera)

    def simulate_outage(self, unavailable: bool) -> None:
        self._unavailable = unavailable

    def get_camera(self, camera_id: str) -> dict:
        return dict(self._require_camera(camera_id))

    def has_camera(self, camera_id: str) -> bool:
        return camera_id in self._cameras


mock_provider = MockCameraProvider(
    provider_id="mock",
    name="Mock Camera Provider",
    manufacturer="MockCam",
    cameras=[
        {
            "id": "mock-front-door", "name": "Front Door", "type": "camera", "model": "MockCam Pro",
            "status": CameraStatus.ONLINE.value, "online": True, "battery_level": None,
            "capabilities": {
                "snapshot": SUPPORTED, "liveStream": SUPPORTED, "recordings": SUPPORTED,
                "motionEvents": SUPPORTED, "personEvents": SUPPORTED, "vehicleEvents": SUPPORTED,
                "doorbellEvents": UNSUPPORTED, "twoWayAudio": UNKNOWN, "battery": UNSUPPORTED, "ptz": UNSUPPORTED,
            },
        },
        {
            "id": "mock-driveway", "name": "Driveway", "type": "camera", "model": "MockCam Pro",
            "status": CameraStatus.ONLINE.value, "online": True, "battery_level": None,
            "capabilities": {
                "snapshot": SUPPORTED, "liveStream": SUPPORTED, "recordings": SUPPORTED,
                "motionEvents": SUPPORTED, "personEvents": UNSUPPORTED, "vehicleEvents": SUPPORTED,
                "doorbellEvents": UNSUPPORTED, "twoWayAudio": UNSUPPORTED, "battery": UNSUPPORTED, "ptz": UNSUPPORTED,
            },
        },
        {
            "id": "mock-backyard", "name": "Backyard", "type": "camera", "model": "MockCam Pro",
            "status": CameraStatus.ONLINE.value, "online": True, "battery_level": None,
            "capabilities": {
                "snapshot": SUPPORTED, "liveStream": SUPPORTED, "recordings": SUPPORTED,
                "motionEvents": SUPPORTED, "personEvents": SUPPORTED, "vehicleEvents": UNSUPPORTED,
                "doorbellEvents": UNSUPPORTED, "twoWayAudio": UNSUPPORTED, "battery": UNSUPPORTED, "ptz": UNSUPPORTED,
            },
        },
        {
            "id": "mock-garden", "name": "Garden", "type": "camera", "model": "MockCam Pro",
            "status": CameraStatus.ONLINE.value, "online": True, "battery_level": None,
            "capabilities": {
                "snapshot": SUPPORTED, "liveStream": SUPPORTED, "recordings": SUPPORTED,
                "motionEvents": SUPPORTED, "personEvents": UNSUPPORTED, "vehicleEvents": UNSUPPORTED,
                "doorbellEvents": UNSUPPORTED, "twoWayAudio": UNSUPPORTED, "battery": UNSUPPORTED, "ptz": UNSUPPORTED,
            },
        },
    ],
)

mock_eufy_provider = MockCameraProvider(
    provider_id="mock-eufy",
    name="Mock Eufy HomeBase",
    manufacturer="Eufy",
    cameras=[
        {
            "id": "mock-eufy-doorbell", "name": "Front Doorbell", "type": "doorbell", "model": "T8210",
            "status": CameraStatus.ONLINE.value, "online": True, "battery_level": 82,
            "capabilities": {
                "snapshot": SUPPORTED, "liveStream": SUPPORTED, "recordings": SUPPORTED,
                "motionEvents": SUPPORTED, "personEvents": SUPPORTED, "vehicleEvents": UNSUPPORTED,
                "doorbellEvents": SUPPORTED, "twoWayAudio": UNKNOWN, "battery": SUPPORTED, "ptz": UNSUPPORTED,
            },
        },
    ],
)

PROVIDERS: list[MockCameraProvider] = [mock_provider, mock_eufy_provider]

