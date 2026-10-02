"""Contract tests for the Eufy edge adapter.

These never contact a real Eufy account, HomeBase, bridge or go2rtc: the
WebSocket client is replaced with a fake, exactly as HomeCam's own Eufy
provider tests use a mocked adapter.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

import app as adapter
from eufy_ws import EufyBridgeError, EufyBridgeUnavailable, _decode_buffer

TOKEN = "test-token"
SERIAL = "T8210P1234567890"
HLS_TOKEN = "hls-path-token"
JPEG = b"\xff\xd8\xff\xe0tiny-jpeg\xff\xd9"


class FakeClient:
    def __init__(self):
        self.devices_payload = [
            {
                "id": SERIAL,
                "name": "Front Doorbell",
                "type": "doorbell",
                "model": "T8210",
                "online": True,
                "battery_level": 82,
                "capabilities": {"snapshot": True, "liveStream": True, "doorbellEvents": True, "battery": True},
            }
        ]
        self.auth_state = "authenticated"
        self.connect_error: Exception | None = None
        self.snapshot_error: Exception | None = None
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.unsubscribed = False

    async def ensure_connected(self):
        if self.connect_error:
            raise self.connect_error

    def has_device(self, serial):
        return serial == SERIAL

    def devices(self):
        return self.devices_payload

    async def snapshot(self, serial):
        if self.snapshot_error:
            raise self.snapshot_error
        return JPEG

    async def start_livestream(self, serial):
        self.started.append(serial)

    async def subscribe(self, serial):
        queue: asyncio.Queue = asyncio.Queue()
        queue.put_nowait(b"\x00\x00\x00\x01frame")
        queue.put_nowait(None)
        return queue

    async def unsubscribe(self, serial, queue):
        self.unsubscribed = True

    async def close(self):
        pass


@pytest.fixture
def fake(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(adapter, "client", client)
    monkeypatch.setattr(adapter.settings, "edge_token", TOKEN)
    monkeypatch.setattr(adapter.settings, "stream_token", "stream-token")
    monkeypatch.setattr(adapter.settings, "hls_public_base_url", None)
    monkeypatch.setattr(adapter.settings, "hls_token", HLS_TOKEN)
    return client


@pytest.fixture
def http(fake):
    with TestClient(adapter.app) as test_client:
        yield test_client


def auth():
    return {"Authorization": f"Bearer {TOKEN}"}


# ----------------------------------------------------------------- auth

def test_healthz_is_unauthenticated_and_leaks_nothing(http):
    response = http.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize("path", ["/health", "/devices", f"/devices/{SERIAL}/snapshot", f"/devices/{SERIAL}/live"])
def test_endpoints_reject_a_missing_token(http, path):
    assert http.get(path).status_code == 401


def test_endpoints_reject_a_wrong_token(http):
    assert http.get("/devices", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_adapter_refuses_to_serve_when_no_token_is_configured(http, fake, monkeypatch):
    monkeypatch.setattr(adapter.settings, "edge_token", None)
    # Refuse rather than silently running unauthenticated on a private LAN.
    assert http.get("/devices", headers=auth()).status_code == 503


# -------------------------------------------------------------- contract

def test_health_reports_auth_state(http, fake):
    response = http.get("/health", headers=auth())
    assert response.status_code == 200
    assert response.json()["auth_state"] == "authenticated"


def test_health_reports_unauthenticated_instead_of_failing_when_bridge_is_down(http, fake):
    fake.connect_error = EufyBridgeUnavailable("bridge down")
    response = http.get("/health", headers=auth())
    # HomeCam renders auth_state to tell the owner local action is needed,
    # so this must be a 200 body rather than a transport error.
    assert response.status_code == 200
    assert response.json()["auth_state"] == "unauthenticated"


def test_devices_uses_the_shape_the_homecam_provider_expects(http):
    payload = http.get("/devices", headers=auth()).json()
    assert "devices" in payload
    device = payload["devices"][0]
    assert device["id"] == SERIAL
    assert device["type"] == "doorbell"
    assert device["battery_level"] == 82


def test_devices_returns_503_when_the_bridge_is_unreachable(http, fake):
    fake.connect_error = EufyBridgeUnavailable("bridge down")
    assert http.get("/devices", headers=auth()).status_code == 503


def test_snapshot_returns_jpeg_bytes(http):
    response = http.get(f"/devices/{SERIAL}/snapshot", headers=auth())
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.content == JPEG


def test_snapshot_rejects_non_jpeg_payloads(http, fake, monkeypatch):
    async def not_an_image(serial):
        return b"<html>login</html>"

    monkeypatch.setattr(fake, "snapshot", not_an_image)
    # Never hand HomeCam's AI pipeline something that is not an image.
    assert http.get(f"/devices/{SERIAL}/snapshot", headers=auth()).status_code == 502


def test_snapshot_404s_before_any_event_image_exists(http, fake):
    fake.snapshot_error = EufyBridgeError("no event image available yet for this device")
    assert http.get(f"/devices/{SERIAL}/snapshot", headers=auth()).status_code == 404


def test_unknown_device_is_404(http):
    assert http.get("/devices/not-a-device/snapshot", headers=auth()).status_code == 404


# ------------------------------------------------------------------ live

def test_live_registers_a_go2rtc_stream_and_returns_its_hls_url(http, fake, monkeypatch):
    calls = []

    async def fake_ensure(serial):
        calls.append(serial)
        return f"eufy-{serial}"

    monkeypatch.setattr(adapter, "_ensure_go2rtc_stream", fake_ensure)
    monkeypatch.setattr(adapter.settings, "go2rtc_public_url", "http://ha.tailnet.ts.net:1984")

    response = http.get(f"/devices/{SERIAL}/live", headers=auth())
    assert response.status_code == 200
    assert response.json() == {
        "hls_url": f"http://ha.tailnet.ts.net:1984/api/stream.m3u8?src=eufy-{SERIAL}"
    }
    assert calls == [SERIAL]
    assert fake.started == [SERIAL]


# ------------------------------------------------------------- HLS relay

@pytest.fixture
def loopback_go2rtc(monkeypatch):
    """go2rtc v1.9.14's HLS routes (internal/hls/hls.go), on loopback."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/stream.m3u8":
            return httpx.Response(
                200,
                text="#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=192000\nhls/playlist.m3u8?id=abc\n",
                headers={"content-type": "application/vnd.apple.mpegurl"},
            )
        if request.url.path == "/api/hls/playlist.m3u8":
            return httpx.Response(200, text="#EXTM3U\nsegment.ts?id=abc&n=1\n")
        if request.url.path == "/api/hls/segment.ts":
            return httpx.Response(200, content=b"\x47ts", headers={"content-type": "video/mp2t"})
        return httpx.Response(404)

    real_client = httpx.AsyncClient

    def make_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(adapter.httpx, "AsyncClient", make_client)
    monkeypatch.setattr(adapter.settings, "go2rtc_url", "http://127.0.0.1:11984")
    return seen


def test_live_advertises_the_token_gated_relay_when_a_public_base_is_set(http, fake, monkeypatch):
    async def fake_ensure(serial):
        return f"eufy-{serial}"

    monkeypatch.setattr(adapter, "_ensure_go2rtc_stream", fake_ensure)
    monkeypatch.setattr(adapter.settings, "hls_public_base_url", "http://ha.tailnet.ts.net:8091")

    response = http.get(f"/devices/{SERIAL}/live", headers=auth())
    assert response.status_code == 200
    url = response.json()["hls_url"]
    assert url == f"http://ha.tailnet.ts.net:8091/hls/{HLS_TOKEN}/eufy-{SERIAL}/stream.m3u8"
    # Never the loopback go2rtc address nor the ffmpeg ingest token.
    assert "11984" not in url and "1984" not in url and "stream-token" not in url


def test_relay_proxies_manifest_and_children_exactly_like_go2rtc(http, loopback_go2rtc):
    base = f"/hls/{HLS_TOKEN}/eufy-{SERIAL}"
    master = http.get(f"{base}/stream.m3u8?src=evil")
    assert master.status_code == 200
    assert "hls/playlist.m3u8?id=abc" in master.text
    assert master.headers["content-type"] == "application/vnd.apple.mpegurl"
    # The stream name always comes from the path, never a caller-supplied src.
    assert loopback_go2rtc[-1].url.params.get_list("src") == [f"eufy-{SERIAL}"]

    # Relative child references resolve under the relay, as players (and the
    # HomeCam API HLS proxy) do.
    playlist = http.get(f"{base}/hls/playlist.m3u8?id=abc")
    assert playlist.status_code == 200
    assert str(loopback_go2rtc[-1].url) == "http://127.0.0.1:11984/api/hls/playlist.m3u8?id=abc"

    segment = http.get(f"{base}/hls/segment.ts?id=abc&n=1")
    assert segment.content == b"\x47ts"
    assert str(loopback_go2rtc[-1].url) == "http://127.0.0.1:11984/api/hls/segment.ts?id=abc&n=1"


@pytest.mark.parametrize(
    "path",
    [
        f"/hls/wrong-token/eufy-{SERIAL}/stream.m3u8",
        f"/hls//eufy-{SERIAL}/stream.m3u8",
        f"/hls/{HLS_TOKEN}/eufy-UNKNOWN/stream.m3u8",
        f"/hls/{HLS_TOKEN}/dahua-1/stream.m3u8",
        f"/hls/{HLS_TOKEN}/eufy-{SERIAL}/hls/../../api/streams",
        f"/hls/{HLS_TOKEN}/eufy-{SERIAL}/hls/config",
        f"/hls/{HLS_TOKEN}/eufy-{SERIAL}/hls/frame.jpeg",
    ],
)
def test_relay_rejects_bad_tokens_unknown_streams_and_non_hls_files(http, loopback_go2rtc, path):
    response = http.get(path)
    assert response.status_code == 404
    assert HLS_TOKEN not in response.text
    # Nothing reached go2rtc: its API (streams, config, exit...) stays private.
    assert loopback_go2rtc == []


def test_relay_reports_go2rtc_outage_without_leaking_its_address(http, monkeypatch):
    def make_client(*args, **kwargs):
        def boom(request):
            raise httpx.ConnectError("refused")

        kwargs["transport"] = httpx.MockTransport(boom)
        return real_client(*args, **kwargs)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(adapter.httpx, "AsyncClient", make_client)
    response = http.get(f"/hls/{HLS_TOKEN}/eufy-{SERIAL}/stream.m3u8")
    assert response.status_code == 503
    assert "127.0.0.1" not in response.text


def test_internal_endpoint_can_be_restricted_to_loopback_clients(http, monkeypatch):
    monkeypatch.setattr(adapter.settings, "internal_loopback_only", True)
    # TestClient's peer is not loopback, so even the right token is refused.
    response = http.get(f"/internal/devices/{SERIAL}/h264?token=stream-token")
    assert response.status_code == 403
    assert adapter._is_loopback("127.0.0.1") and adapter._is_loopback("::1")
    assert not adapter._is_loopback("192.168.2.10") and not adapter._is_loopback("testclient")


def test_access_log_lines_never_contain_hls_or_stream_tokens():
    import logging

    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1,
        '%s - "%s %s HTTP/%s" %d',
        (
            "100.64.0.1:5000", "GET",
            f"/hls/{HLS_TOKEN}/eufy-{SERIAL}/hls/segment.ts?id=abc&n=1",
            "1.1", 200,
        ),
        None,
    )
    adapter.RedactSecretsFilter().filter(record)
    line = record.getMessage()
    assert HLS_TOKEN not in line
    assert f"/hls/***/eufy-{SERIAL}/hls/segment.ts?id=abc&n=1" in line

    ingest = adapter.redact(f"GET /internal/devices/{SERIAL}/h264?token=stream-token HTTP/1.1")
    assert "stream-token" not in ingest
    assert ingest.endswith("?token=*** HTTP/1.1")
    # The ffmpeg source URL as go2rtc might echo it.
    assert "stream-token" not in adapter.redact("src=ffmpeg:http://127.0.0.1:8091/x?a=1&token=stream-token#video=copy")
    # httpx INFO-logs the PATCH registration URL with the source URL-encoded.
    encoded = adapter.redact(
        "HTTP Request: PATCH http://127.0.0.1:11984/api/streams?name=eufy-X"
        "&src=ffmpeg%3Ahttp%3A%2F%2F127.0.0.1%3A8091%2Finternal%2Fdevices%2FX%2Fh264%3Ftoken%3Dstream-token%23video%3Dcopy"
    )
    assert "stream-token" not in encoded
    full_url = adapter.redact(f"GET http://ha.ts.net:8091/hls/{HLS_TOKEN}/eufy-X/stream.m3u8")
    assert HLS_TOKEN not in full_url

    # httpx passes an httpx.URL object (not a str) as the log argument.
    httpx_record = logging.LogRecord(
        "httpx", logging.INFO, __file__, 1, 'HTTP Request: %s %s "%s %d %s"',
        ("PATCH", httpx.URL("http://127.0.0.1:11984/api/streams", params={"src": "ffmpeg:http://a/h264?token=stream-token"}), "HTTP/1.1", 200, "OK"),
        None,
    )
    adapter.RedactSecretsFilter().filter(httpx_record)
    assert "stream-token" not in httpx_record.getMessage()


def test_internal_h264_endpoint_requires_the_stream_token(http):
    assert http.get(f"/internal/devices/{SERIAL}/h264").status_code == 401
    assert http.get(f"/internal/devices/{SERIAL}/h264?token=wrong").status_code == 401


def test_internal_h264_streams_chunks_and_releases_the_p2p_session(http, fake):
    response = http.get(f"/internal/devices/{SERIAL}/h264?token=stream-token")
    assert response.status_code == 200
    assert response.content == b"\x00\x00\x00\x01frame"
    # Unsubscribing is what lets the battery doorbell go back to sleep.
    assert fake.unsubscribed is True


# ---------------------------------------------------------- buffer decode

def test_decode_buffer_handles_node_buffer_json():
    assert _decode_buffer({"type": "Buffer", "data": [1, 2, 3]}) == b"\x01\x02\x03"


def test_decode_buffer_handles_base64_and_data_urls():
    assert _decode_buffer("aGk=") == b"hi"
    assert _decode_buffer("data:image/jpeg;base64,aGk=") == b"hi"


def test_decode_buffer_is_safe_on_junk():
    assert _decode_buffer(None) == b""
    assert _decode_buffer({}) == b""
