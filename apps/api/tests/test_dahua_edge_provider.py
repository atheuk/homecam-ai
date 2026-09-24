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


@pytest.mark.asyncio
async def test_a_camera_that_delivers_media_is_not_refused_for_being_flagged_offline():
    """Measured against the deployed NVR: the connector reported channel 2
    offline while it returned a real 1.5MB JPEG. Gating the fetch on that
    flag was self-fulfilling -- we refused to ask, so we never learned the
    camera was fine."""
    provider = DahuaEdgeProvider(
        DahuaEdgeSettings(base_url="https://edge.tailnet", token="test-token"),
        transport=dahua_edge_transport(online=False),
    )

    assert await provider.get_snapshot("dahua-channel-1") == b"DAHUA-EDGE-SNAPSHOT"

    cameras = {c["id"]: c["online"] for c in await provider.discover_devices()}
    assert cameras["dahua-channel-1"] is True, "real media must count as proof the camera is live"
    assert cameras["dahua-channel-2"] is False, "a channel that never delivered media stays offline"


def _verify_transport(calls: list) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/channels":
            return httpx.Response(
                200,
                json={
                    "channels": [
                        {"channel": 1, "name": "Front", "type": "camera", "online": False},
                        {"channel": 4, "name": "Empty", "type": "camera", "online": False},
                    ]
                },
            )
        if request.url.path == "/channels/1/snapshot":
            return httpx.Response(200, content=b"REAL-JPEG")
        if request.url.path == "/channels/4/snapshot":
            return httpx.Response(503, json={"detail": "no camera"})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_a_wrongly_flagged_camera_is_rediscovered_without_hammering_the_nvr():
    """The web app hides offline cameras, so it never asks them for media.
    The provider must therefore confirm for itself -- but at a bounded rate,
    because each check costs one of the NVR's ~1-2 CGI sessions."""
    calls: list = []
    provider = DahuaEdgeProvider(
        DahuaEdgeSettings(base_url="https://edge.tailnet", token="test-token"),
        transport=_verify_transport(calls),
    )

    await provider.discover_devices()
    await provider.discover_devices()
    cameras = {c["id"]: c["online"] for c in await provider.discover_devices()}

    assert cameras["dahua-channel-1"] is True, "a channel that delivers a real JPEG must come back"
    assert cameras["dahua-channel-4"] is False, "a channel with no camera stays offline"
    # channel 1 is verified once and then held online by the grace window;
    # channel 4 gets a single verification round (its retry is _request's
    # own) and is not re-probed again until the interval lapses.
    assert calls.count("/channels/1/snapshot") == 1, calls
    assert calls.count("/channels/4/snapshot") <= 2, calls
