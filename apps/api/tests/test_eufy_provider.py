import httpx
import pytest

from app.providers.eufy import EufyEdgeProvider, EufySettings


def eufy_transport(auth_state: str = "authenticated") -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"auth_state": auth_state})
        if request.url.path == "/devices":
            return httpx.Response(
                200,
                json={
                    "devices": [
                        {
                            "id": "T8210P123",
                            "name": "Front Doorbell",
                            "type": "doorbell",
                            "model": "T8210",
                            "online": True,
                            "battery_level": 82,
                            "capabilities": {
                                "snapshot": True,
                                "liveStream": True,
                                "motionEvents": True,
                                "personEvents": True,
                                "doorbellEvents": True,
                                "battery": True,
                                "eventImages": True,
                            },
                        }
                    ]
                },
            )
        if request.url.path == "/devices/T8210P123/snapshot":
            return httpx.Response(200, content=b"EUFY-SNAPSHOT")
        if request.url.path == "/devices/T8210P123/live":
            return httpx.Response(200, json={"hls_url": "http://127.0.0.1:8090/eufy/T8210P123.m3u8"})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def eufy_reauth_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(409, json={"auth_state": "reauth_required"})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_eufy_edge_provider_maps_adapter_devices_and_capabilities():
    provider = EufyEdgeProvider(
        EufySettings(adapter_url="http://127.0.0.1:8090", adapter_token="test-token"),
        transport=eufy_transport(),
    )

    cameras = await provider.discover_devices()
    assert cameras[0]["id"] == "eufy-T8210P123"
    assert cameras[0]["capabilities"]["doorbellEvents"] == "SUPPORTED"
    assert cameras[0]["capabilities"]["recordings"] == "UNAVAILABLE"
    assert cameras[0]["battery_level"] == 82
    assert await provider.get_snapshot("eufy-T8210P123") == b"EUFY-SNAPSHOT"
    assert await provider.get_live_stream("eufy-T8210P123") == "http://127.0.0.1:8090/eufy/T8210P123.m3u8"


@pytest.mark.asyncio
async def test_eufy_health_surfaces_reauth_state_without_credentials():
    provider = EufyEdgeProvider(EufySettings(adapter_url="http://127.0.0.1:8090"), transport=eufy_transport("2fa_required"))

    health = await provider.get_health()
    assert health["status"] == "DEGRADED"
    assert "2fa_required" in health["message"]


@pytest.mark.asyncio
async def test_eufy_provider_without_adapter_degrades_gracefully():
    provider = EufyEdgeProvider(EufySettings())

    assert await provider.discover_devices() == []
    health = await provider.get_health()
    assert health["status"] == "DEGRADED"
    assert "EUFY_ADAPTER_URL" in health["message"]


@pytest.mark.asyncio
async def test_eufy_adapter_reauth_failure_is_structured_offline_health():
    provider = EufyEdgeProvider(EufySettings(adapter_url="http://127.0.0.1:8090"), transport=eufy_reauth_transport())

    health = await provider.get_health()
    assert health["status"] == "OFFLINE"
    assert "re-authentication" in health["message"]
