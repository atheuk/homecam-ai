import httpx
import pytest

from app.providers.dahua import DahuaEdgeProvider, DahuaEdgeSettings
from app.providers.dahua import edge_provider


def dahua_edge_transport(dahua_reachable: bool = True, online: bool = True) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("Authorization") != "Bearer test-token":
            return httpx.Response(401)
        if request.url.path == "/health":
            return httpx.Response(200, json={"dahua_reachable": dahua_reachable, "message": "ok"})
        if request.url.path == "/channels":
            return httpx.Response(
                200,
                json={
                    "channels": [
                        {"channel": 1, "name": "Front Door", "type": "camera", "online": online},
                        {"channel": 2, "name": "Back Yard", "type": "camera", "online": online},
                    ]
                },
            )
        if request.url.path == "/channels/1/snapshot":
            return httpx.Response(200, content=b"DAHUA-EDGE-SNAPSHOT")
        if request.url.path == "/channels/1/live":
            return httpx.Response(200, json={"kind": "hls", "url": "https://edge.tailnet/hls/1.m3u8"})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def dahua_edge_unreachable_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"dahua_reachable": False, "message": "connection refused"})
        if request.url.path == "/channels":
            return httpx.Response(200, json={"channels": []})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_dahua_edge_provider_maps_channels_and_capabilities():
    provider = DahuaEdgeProvider(
        DahuaEdgeSettings(base_url="https://edge.tailnet", token="test-token"),
        transport=dahua_edge_transport(),
    )

    cameras = await provider.discover_devices()
    assert [c["id"] for c in cameras] == ["dahua-channel-1", "dahua-channel-2"]
    assert cameras[0]["capabilities"]["snapshot"] == "SUPPORTED"
    assert cameras[0]["capabilities"]["recordings"] == "UNAVAILABLE"

    assert await provider.get_snapshot("dahua-channel-1") == b"DAHUA-EDGE-SNAPSHOT"
    stream_url = await provider.get_live_stream("dahua-channel-1")
    assert stream_url == "https://edge.tailnet/hls/1.m3u8"
    # Never leaks raw Dahua RTSP credentials/host to the caller in edge mode.
    assert "rtsp://" not in stream_url
    assert "@" not in stream_url


@pytest.mark.asyncio
async def test_dahua_edge_health_reports_fully_online():
    provider = DahuaEdgeProvider(
        DahuaEdgeSettings(base_url="https://edge.tailnet", token="test-token"),
        transport=dahua_edge_transport(),
    )

    health = await provider.get_health()
    assert health["status"] == "ONLINE"
    assert health["camera_count"] == 2
    assert health["online_camera_count"] == 2


@pytest.mark.asyncio
async def test_dahua_edge_health_distinguishes_reachable_edge_from_unreachable_dahua():
    provider = DahuaEdgeProvider(
        DahuaEdgeSettings(base_url="https://edge.tailnet", token="test-token"),
        transport=dahua_edge_unreachable_transport(),
    )

    health = await provider.get_health()
    assert health["status"] == "DEGRADED"
    assert "no Dahua channels" in health["message"]


@pytest.mark.asyncio
async def test_dahua_edge_provider_without_base_url_degrades_gracefully_and_mentions_qr_limitation():
    provider = DahuaEdgeProvider(DahuaEdgeSettings())

    assert await provider.discover_devices() == []
    health = await provider.get_health()
    assert health["status"] == "DEGRADED"
    assert "QR/DMSS P2P serial pairing" in health["message"]
    assert "Raspberry Pi" in health["message"]


@pytest.mark.asyncio
async def test_dahua_edge_provider_rejects_bad_token_as_offline():
    provider = DahuaEdgeProvider(
        DahuaEdgeSettings(base_url="https://edge.tailnet", token="wrong-token"),
        transport=dahua_edge_transport(),
    )

    health = await provider.get_health()
    assert health["status"] == "OFFLINE"


@pytest.mark.asyncio
async def test_dahua_edge_provider_uses_dedicated_tailscale_proxy(monkeypatch):
    captured: dict = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _url, headers):
            assert headers["Authorization"]
            return httpx.Response(200, json={"channels": []})

    monkeypatch.setenv("TAILSCALE_HTTP_PROXY", "http://127.0.0.1:1055")
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    provider = DahuaEdgeProvider(DahuaEdgeSettings(base_url="https://homecam-edge.example.ts.net", token="token"))
    await provider.discover_devices()

    assert captured["proxy"] == "http://127.0.0.1:1055"


def _flapping_transport(state: dict) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/channels":
            return httpx.Response(
                200,
                json={
                    "channels": [
                        {"channel": 1, "name": "Front Door", "type": "camera", "online": state["online"]},
                        {"channel": 4, "name": "Disconnected", "type": "camera", "online": False},
                    ]
                },
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_a_briefly_flapping_channel_keeps_its_online_status():
    """Regression measured against the deployed system: the NVR behind the
    edge connector refuses a CGI session whenever it is busy, so channels
    that were streaming fine flipped to offline and back, hiding working
    cameras from the web app."""
    state = {"online": True}
    provider = DahuaEdgeProvider(
        DahuaEdgeSettings(base_url="https://edge.tailnet", token="test-token"),
        transport=_flapping_transport(state),
    )

    async def status() -> dict[str, bool]:
        return {c["id"]: c["online"] for c in await provider.discover_devices()}

    assert await status() == {"dahua-channel-1": True, "dahua-channel-4": False}

    state["online"] = False
    assert await status() == {"dahua-channel-1": True, "dahua-channel-4": False}, (
        "a channel confirmed online moments ago must survive a blip, and a channel "
        "never seen online must not be invented"
    )

    state["online"] = True
    assert await status() == {"dahua-channel-1": True, "dahua-channel-4": False}


@pytest.mark.asyncio
async def test_a_channel_that_stays_offline_past_the_grace_window_goes_offline(monkeypatch):
    state = {"online": True}
    provider = DahuaEdgeProvider(
        DahuaEdgeSettings(base_url="https://edge.tailnet", token="test-token"),
        transport=_flapping_transport(state),
    )
    assert (await provider.discover_devices())[0]["online"] is True

    state["online"] = False
    monkeypatch.setattr(edge_provider, "EDGE_OFFLINE_GRACE_SECONDS", 0.0)
    assert (await provider.discover_devices())[0]["online"] is False
