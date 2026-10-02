import asyncio

import httpx
import pytest

from app.providers.eufy import EufyEdgeProvider, EufySettings
from app.providers.base import ProviderUnavailableError


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


@pytest.mark.asyncio
@pytest.mark.parametrize("proxy_url", [None, "http://127.0.0.1:1055"])
async def test_eufy_requests_use_tailscale_proxy(monkeypatch, proxy_url):
    if proxy_url:
        monkeypatch.setenv("TAILSCALE_HTTP_PROXY", proxy_url)
    else:
        monkeypatch.delenv("TAILSCALE_HTTP_PROXY", raising=False)
    real_client = httpx.AsyncClient
    calls = []

    def client_factory(**kwargs):
        calls.append(kwargs.copy())
        kwargs.pop("proxy", None)
        kwargs.pop("transport", None)
        return real_client(**kwargs, transport=eufy_transport())

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    provider = EufyEdgeProvider(
        EufySettings(adapter_url="http://edge.example.ts.net:8091", adapter_token="test-token")
    )
    assert (await provider.get_health())["status"] == "ONLINE"
    assert await provider.get_snapshot("eufy-T8210P123") == b"EUFY-SNAPSHOT"
    assert await provider.get_live_stream("eufy-T8210P123")
    assert len(calls) == 4
    for options in calls:
        assert options.get("proxy") == proxy_url
        assert options["follow_redirects"] is False


@pytest.mark.asyncio
async def test_eufy_explicit_transport_bypasses_tailscale_proxy(monkeypatch):
    monkeypatch.setenv("TAILSCALE_HTTP_PROXY", "http://127.0.0.1:1")
    provider = EufyEdgeProvider(
        EufySettings(adapter_url="http://edge.example.ts.net:8091"),
        transport=eufy_transport(),
    )
    assert (await provider.get_health())["status"] == "ONLINE"


@pytest.mark.asyncio
async def test_event_snapshot_reads_existing_picture_even_when_camera_is_offline():
    requests = []
    jpeg = b"\xff\xd8event-picture\xff\xd9"

    def handler(request):
        requests.append(request)
        if request.url.path == "/devices":
            return httpx.Response(200, json={"devices": [{"id": "T8210P123", "online": False}]})
        return httpx.Response(200, content=jpeg)

    provider = EufyEdgeProvider(
        EufySettings(adapter_url="http://edge", adapter_token="test-token"),
        transport=httpx.MockTransport(handler),
    )
    assert await provider.get_event_snapshot("eufy-T8210P123") == jpeg
    assert [request.url.path for request in requests] == ["/devices", "/devices/T8210P123/snapshot"]
    assert all(request.headers["Authorization"] == "Bearer test-token" for request in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "content", "expected"),
    [(404, b"", None), (200, b"<html>not an image</html>", "error"), (302, b"", "error")],
)
async def test_event_snapshot_handles_missing_invalid_and_redirected_images(status, content, expected):
    requests = []

    def handler(request):
        requests.append(request.url.path)
        if request.url.path == "/devices":
            return httpx.Response(200, json={"devices": [{"id": "T8210P123"}]})
        return httpx.Response(status, content=content, headers={"location": "http://untrusted/image"})

    provider = EufyEdgeProvider(
        EufySettings(adapter_url="http://edge"), transport=httpx.MockTransport(handler)
    )
    if expected == "error":
        with pytest.raises(ProviderUnavailableError):
            await provider.get_event_snapshot("eufy-T8210P123")
    else:
        assert await provider.get_event_snapshot("eufy-T8210P123") is None
    assert requests == ["/devices", "/devices/T8210P123/snapshot"]


@pytest.mark.asyncio
async def test_event_snapshot_is_cancelable_by_parent_timeout():
    canceled = asyncio.Event()
    requests = []

    async def handler(request):
        requests.append(request.url.path)
        if request.url.path == "/devices":
            return httpx.Response(200, json={"devices": [{"id": "T8210P123"}]})
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            canceled.set()
            raise

    provider = EufyEdgeProvider(
        EufySettings(adapter_url="http://edge"), transport=httpx.MockTransport(handler)
    )
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(provider.get_event_snapshot("eufy-T8210P123"), timeout=0.02)
    assert canceled.is_set()
    assert requests == ["/devices", "/devices/T8210P123/snapshot"]


def eufy_clip_transport(event_clips: bool, seen: list) -> httpx.MockTransport:
    base = eufy_transport()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/devices":
            payload = base.handler(request).json() if hasattr(base, "handler") else None
            if payload is None:
                raise AssertionError("unexpected transport shape")
            payload["devices"][0]["capabilities"]["eventClips"] = event_clips
            return httpx.Response(200, json=payload)
        if request.url.path == "/devices/T8210P123/clips":
            return httpx.Response(200, json={"clips": [
                {"id": "c1", "started_at": "2025-01-01T10:00:00+00:00", "duration_seconds": 14.8,
                 "pre_roll_seconds": 0.0, "trigger": "doorbell", "complete": True, "failed": None},
                {"started_at": "missing id"},
            ]})
        if request.url.raw_path == b"/devices/T8210P123/clips/c%2F1":
            return httpx.Response(200, content=b"\x00\x00\x00\x18ftypisom-video")
        if request.url.path == "/devices/T8210P123/clips/bad":
            return httpx.Response(200, content=b"<html>")
        return base.handler(request)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_eufy_event_clips_are_listed_and_fetched_with_the_adapter_token():
    seen: list = []
    provider = EufyEdgeProvider(
        EufySettings(adapter_url="http://127.0.0.1:8090", adapter_token="test-token"),
        transport=eufy_clip_transport(True, seen),
    )
    cameras = await provider.discover_devices()
    assert cameras[0]["capabilities"]["eventClips"] == "SUPPORTED"
    clips = await provider.list_event_clips("eufy-T8210P123")
    assert clips == [{
        "id": "c1", "started_at": "2025-01-01T10:00:00+00:00", "duration_seconds": 14.8,
        "pre_roll_seconds": 0.0, "trigger": "doorbell", "complete": True,
    }]
    video = await provider.get_event_clip("eufy-T8210P123", "c/1")
    assert video[4:8] == b"ftyp"
    with pytest.raises(ProviderUnavailableError):
        await provider.get_event_clip("eufy-T8210P123", "bad")
    assert all(r.headers.get("authorization") == "Bearer test-token" for r in seen)


@pytest.mark.asyncio
async def test_eufy_event_clips_unsupported_on_older_adapters():
    seen: list = []
    provider = EufyEdgeProvider(
        EufySettings(adapter_url="http://127.0.0.1:8090", adapter_token="test-token"),
        transport=eufy_clip_transport(False, seen),
    )
    cameras = await provider.discover_devices()
    assert cameras[0]["capabilities"]["eventClips"] == "UNAVAILABLE"
    assert await provider.list_event_clips("eufy-T8210P123") is None
    assert not any(r.url.path.endswith("/clips") for r in seen)
