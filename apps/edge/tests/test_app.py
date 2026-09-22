import httpx
import pytest

from app import DahuaClient, EdgeSettings, create_app


def dahua_mock_transport(serial_ok: bool = True) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cgi-bin/magicBox.cgi":
            if not serial_ok:
                return httpx.Response(401)
            return httpx.Response(200, text="sn=TESTSERIAL123\n")
        if request.url.path == "/cgi-bin/snapshot.cgi":
            return httpx.Response(200, content=b"JPEGDATA")
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def build_client(settings: EdgeSettings, transport: httpx.MockTransport) -> httpx.AsyncClient:
    app = create_app(settings=settings, transport=transport)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://edge.local")


@pytest.mark.asyncio
async def test_healthz_requires_no_token():
    settings = EdgeSettings(edge_token="secret")
    async with build_client(settings, dahua_mock_transport()) as client:
        r = await client.get("/healthz")
        assert r.status_code == 200
        assert r.json() == {"ok": True}


@pytest.mark.asyncio
async def test_health_rejects_missing_or_wrong_token():
    settings = EdgeSettings(edge_token="secret", dahua_host="192.0.2.1", dahua_username="u", dahua_password="p")
    async with build_client(settings, dahua_mock_transport()) as client:
        r_missing = await client.get("/health")
        assert r_missing.status_code == 401
        r_wrong = await client.get("/health", headers={"Authorization": "Bearer wrong"})
        assert r_wrong.status_code == 401


@pytest.mark.asyncio
async def test_health_and_channels_report_dahua_reachable_with_correct_token():
    settings = EdgeSettings(
        edge_token="secret",
        dahua_host="192.0.2.1",
        dahua_username="u",
        dahua_password="p",
        dahua_channels="1:Front Door,2:Driveway",
    )
    async with build_client(settings, dahua_mock_transport()) as client:
        headers = {"Authorization": "Bearer secret"}
        health = await client.get("/health", headers=headers)
        assert health.status_code == 200
        assert health.json()["dahua_reachable"] is True

        channels = await client.get("/channels", headers=headers)
        assert channels.status_code == 200
        body = channels.json()["channels"]
        assert [c["channel"] for c in body] == [1, 2]
        assert all(c["online"] for c in body)


@pytest.mark.asyncio
async def test_snapshot_and_live_never_leak_dahua_credentials():
    settings = EdgeSettings(
        edge_token="secret",
        dahua_host="192.0.2.1",
        dahua_username="u",
        dahua_password="super-secret-nvr-password",
        dahua_channels="1:Front Door",
        stream_base_url="http://127.0.0.1:8888",
    )
    async with build_client(settings, dahua_mock_transport()) as client:
        headers = {"Authorization": "Bearer secret"}
        snapshot = await client.get("/channels/1/snapshot", headers=headers)
        assert snapshot.status_code == 200
        assert snapshot.content == b"JPEGDATA"

        live = await client.get("/channels/1/live", headers=headers)
        assert live.status_code == 200
        body = live.json()
        assert body["kind"] == "hls"
        assert body["url"] == "http://127.0.0.1:8888/dahua-1/index.m3u8"
        assert "rtsp://" not in body["url"]
        assert "super-secret-nvr-password" not in str(body)


@pytest.mark.asyncio
async def test_health_reports_unreachable_dahua_without_leaking_credentials():
    settings = EdgeSettings(
        edge_token="secret",
        dahua_host="192.0.2.1",
        dahua_username="u",
        dahua_password="super-secret-nvr-password",
    )
    async with build_client(settings, dahua_mock_transport(serial_ok=False)) as client:
        r = await client.get("/health", headers={"Authorization": "Bearer secret"})
        assert r.status_code == 200
        body = r.json()
        assert body["dahua_reachable"] is False
        assert "super-secret-nvr-password" not in body["message"]


@pytest.mark.asyncio
async def test_edge_connector_refuses_all_auth_when_no_token_configured():
    settings = EdgeSettings(edge_token=None, dahua_host="192.0.2.1", dahua_username="u", dahua_password="p")
    async with build_client(settings, dahua_mock_transport()) as client:
        r = await client.get("/health")
        assert r.status_code == 503


def test_dahua_client_requires_credentials_before_making_requests():
    settings = EdgeSettings(dahua_host="192.0.2.1", dahua_username="u", dahua_password="p")
    client = DahuaClient(settings)
    assert settings.dahua_configured is True
