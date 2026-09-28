"""Contract tests for the Eufy edge adapter.

These never contact a real Eufy account, HomeBase, bridge or go2rtc: the
WebSocket client is replaced with a fake, exactly as HomeCam's own Eufy
provider tests use a mocked adapter.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

import app as adapter
from eufy_ws import EufyBridgeError, EufyBridgeUnavailable, _decode_buffer

TOKEN = "test-token"
SERIAL = "T8210P1234567890"
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
    adapter._registered_streams.clear()
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
