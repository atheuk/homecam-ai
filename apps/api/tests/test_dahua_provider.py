import httpx
import pytest

from app.providers.base import CameraNotFoundError
from app.providers.dahua import DahuaProvider, DahuaSettings


def dahua_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cgi-bin/magicBox.cgi":
            return httpx.Response(200, text="sn=5J006FCPAZ6B52A\n")
        if request.url.path == "/cgi-bin/snapshot.cgi":
            assert request.url.params["channel"] == "1"
            return httpx.Response(200, content=b"JPEG-BYTES", headers={"content-type": "image/jpeg"})
        if request.url.path == "/cgi-bin/storage.cgi":
            return httpx.Response(200, text="list[0].State=Normal")
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def dahua_auth_failure_transport() -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: httpx.Response(401, text="Unauthorized"))


@pytest.mark.asyncio
async def test_dahua_provider_discovers_configured_channels_and_snapshot():
    provider = DahuaProvider(
        DahuaSettings(
            host="192.0.2.10",
            username="local-user",
            password="local-pass",
            channels="1:Front Door",
        ),
        transport=dahua_transport(),
    )

    cameras = await provider.discover_devices()
    assert cameras[0]["id"] == "dahua-channel-1"
    assert cameras[0]["capabilities"]["snapshot"] == "SUPPORTED"
    assert await provider.get_snapshot("dahua-channel-1") == b"JPEG-BYTES"
    assert await provider.get_live_stream("dahua-channel-1") == (
        "rtsp://192.0.2.10:554/cam/realmonitor?channel=1&subtype=0"
    )


@pytest.mark.asyncio
async def test_dahua_health_checks_expected_serial():
    provider = DahuaProvider(
        DahuaSettings(
            host="192.0.2.10",
            username="local-user",
            password="local-pass",
            channels="1:Front Door",
        ),
        transport=dahua_transport(),
    )

    health = await provider.get_health()
    assert health["status"] == "ONLINE"
    assert "5J006FCPAZ6B52A" in health["message"]


@pytest.mark.asyncio
async def test_dahua_provider_without_host_degrades_gracefully():
    provider = DahuaProvider(DahuaSettings(channels="1:Front Door"))

    assert await provider.discover_devices() == []
    health = await provider.get_health()
    assert health["status"] == "DEGRADED"
    assert "LAN host" in health["message"]


@pytest.mark.asyncio
async def test_dahua_unknown_camera_raises_not_found():
    provider = DahuaProvider(DahuaSettings(channels="1:Front Door"))

    with pytest.raises(CameraNotFoundError):
        await provider.get_capabilities("dahua-channel-2")


@pytest.mark.asyncio
async def test_dahua_auth_failure_is_structured_offline_health():
    provider = DahuaProvider(
        DahuaSettings(
            host="192.0.2.10",
            username="local-user",
            password="local-pass",
            channels="1:Front Door",
        ),
        transport=dahua_auth_failure_transport(),
    )

    health = await provider.get_health()
    assert health["status"] == "OFFLINE"
    assert "authentication rejected" in health["message"]
