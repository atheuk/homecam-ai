"""Protocol regression tests for the eufy-security-ws client and go2rtc wiring.

A fake WebSocket speaks the eufy-security-ws schema >= 13 wire format; go2rtc
is an httpx MockTransport. Nothing here touches a real bridge, account,
device or go2rtc instance.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi import HTTPException

import app as adapter
import eufy_ws
from eufy_ws import EufyWsClient, Livestream

SERIAL = "T8210P1234567890"
JPEG = b"\xff\xd8\xff\xe0tiny-jpeg\xff\xd9"
SPS = b"\x00\x00\x00\x01\x67sps"
PPS = b"\x00\x00\x00\x01\x68pps"
IDR = b"\x00\x00\x00\x01\x65idr"
P_FRAME = b"\x00\x00\x00\x01\x41p"

DOORBELL_PROPERTIES = {
    "serialNumber": SERIAL,
    "name": "Front Doorbell",
    "model": "T8210",
    "type": 5,
    "battery": 88,
    "enabled": True,
    "motionDetected": False,
    "personDetected": False,
    "picture": {"data": {"type": "Buffer", "data": list(JPEG)}, "type": {"ext": "jpg", "mime": "image/jpeg"}},
}


class FakeBridge:
    """Minimal eufy-security-ws server speaking schema >= 13."""

    def __init__(self, devices: list[str] | None = None) -> None:
        self.devices = list(devices if devices is not None else [SERIAL])
        self.sent: list[dict] = []
        self.incoming: asyncio.Queue = asyncio.Queue()

    async def send(self, raw: str) -> None:
        message = json.loads(raw)
        self.sent.append(message)
        command = message["command"]
        if command == "set_api_schema":
            result: dict = {}
        elif command == "start_listening":
            # Schema >= 13: serial numbers only, no properties.
            result = {"state": {"driver": {"connected": True}, "stations": [], "devices": list(self.devices)}}
        elif command == "device.get_properties":
            result = {"serialNumber": message["serialNumber"], "properties": dict(DOORBELL_PROPERTIES)}
        else:
            result = {"async": True}
        self.push({"type": "result", "messageId": message["messageId"], "success": True, "result": result})

    def push(self, message: dict) -> None:
        self.incoming.put_nowait(json.dumps(message))

    def commands(self, name: str) -> list[dict]:
        return [m for m in self.sent if m["command"] == name]

    async def close(self) -> None:
        self.incoming.put_nowait(None)

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        item = await self.incoming.get()
        if item is None:
            raise StopAsyncIteration
        return item


@pytest.fixture
def bridge(monkeypatch):
    holder: dict = {}

    async def connect(url, **kwargs):
        return holder["bridge"]

    monkeypatch.setattr(eufy_ws.websockets, "connect", connect)
    return holder


def run(coro):
    return asyncio.run(coro)


def test_serial_only_state_is_enriched_with_device_properties(bridge):
    async def scenario():
        bridge["bridge"] = fake = FakeBridge()
        client = EufyWsClient("ws://bridge")
        await client.ensure_connected()
        try:
            assert [c["serialNumber"] for c in fake.commands("device.get_properties")] == [SERIAL]
            (device,) = client.devices()
            assert device["name"] == "Front Doorbell"
            assert device["type"] == "doorbell"
            assert device["battery_level"] == 88
            assert device["capabilities"]["snapshot"] is True
            assert device["capabilities"]["doorbellEvents"] is True
            assert await client.snapshot(SERIAL) == JPEG
        finally:
            await client.close()

    run(scenario())


def test_snapshot_uses_latest_event_picture_without_waking_camera(bridge):
    async def scenario():
        bridge["bridge"] = fake = FakeBridge()
        client = EufyWsClient("ws://bridge")
        await client.ensure_connected()
        try:
            for event in ("ring", "motion"):
                latest = b"\xff\xd8" + event.encode() + b"\xff\xd9"
                client._handle_event({
                    "event": "property changed", "serialNumber": SERIAL,
                    "name": "picture",
                    "value": {"type": "Buffer", "data": list(latest)},
                })
                assert await client.snapshot(SERIAL) == latest
            assert [message["command"] for message in fake.sent] == [
                "set_api_schema", "start_listening", "device.get_properties"
            ]
            assert client._livestreams == {}
        finally:
            await client.close()

    run(scenario())


def test_snapshot_never_downloads_picture_urls_or_starts_live_video(bridge):
    async def scenario():
        bridge["bridge"] = fake = FakeBridge()
        client = EufyWsClient("ws://bridge")
        await client.ensure_connected()
        try:
            client._properties[SERIAL] = {"pictureUrl": "http://127.0.0.1/private"}

            async def refresh(serial):
                assert serial == SERIAL

            client.refresh_properties = refresh
            with pytest.raises(eufy_ws.EufyBridgeError, match="no event image"):
                await client.snapshot(SERIAL)
            assert not fake.commands("device.start_livestream")
            assert client._livestreams == {}
        finally:
            await client.close()

    run(scenario())


def test_devices_added_after_connect_are_picked_up(bridge):
    async def scenario():
        # Bridge still logging in to Eufy: no devices in the initial state.
        bridge["bridge"] = fake = FakeBridge(devices=[])
        client = EufyWsClient("ws://bridge")
        await client.ensure_connected()
        try:
            assert client.devices() == []
            fake.push({"type": "event", "event": {"source": "device", "event": "device added", "device": SERIAL}})
            for _ in range(50):
                await asyncio.sleep(0)
                if client.devices() and client.devices()[0]["type"] == "doorbell":
                    break
            assert client.has_device(SERIAL)
            assert client.devices()[0]["name"] == "Front Doorbell"

            fake.push({"type": "event", "event": {"source": "device", "event": "device removed", "device": SERIAL}})
            for _ in range(50):
                await asyncio.sleep(0)
                if not client.has_device(SERIAL):
                    break
            assert not client.has_device(SERIAL)
        finally:
            await client.close()

    run(scenario())


def test_late_subscriber_receives_the_cached_keyframe_first():
    async def scenario():
        stream = Livestream()
        stream.publish(P_FRAME)  # mid-GOP junk before any IDR: never replayed
        stream.publish(SPS + PPS + IDR)
        stream.publish(P_FRAME)
        queue = stream.add_subscriber()
        stream.publish(P_FRAME)
        received = [queue.get_nowait() for _ in range(queue.qsize())]
        assert received == [SPS + PPS + IDR, P_FRAME, P_FRAME]

        stream.publish(SPS + PPS + IDR)  # new GOP resets the cache
        assert stream.gop == [SPS + PPS + IDR]

    run(scenario())


def test_sparse_parameter_sets_are_replayed_before_a_later_idr():
    # Cameras may send SPS/PPS once at stream start and then only IDRs.
    stream = Livestream()
    stream.publish(SPS + PPS + IDR)
    stream.publish(P_FRAME)
    stream.publish(IDR)  # next GOP without parameter sets
    stream.publish(P_FRAME)
    queue = stream.add_subscriber()
    received = [queue.get_nowait() for _ in range(queue.qsize())]
    assert received == [SPS, PPS, IDR, P_FRAME]


def test_separately_sent_parameter_sets_are_kept():
    stream = Livestream()
    stream.publish(SPS)
    stream.publish(PPS)
    stream.publish(IDR)
    queue = stream.add_subscriber()
    assert [queue.get_nowait() for _ in range(queue.qsize())] == [SPS, PPS, IDR]


def test_gop_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(eufy_ws, "GOP_CACHE_MAX_BYTES", 32)
    stream = Livestream()
    stream.publish(SPS + PPS + IDR)
    for _ in range(10):
        stream.publish(P_FRAME)
    assert stream.gop == []
    assert stream.add_subscriber().empty()


def test_gop_cache_recovers_at_next_idr_after_overflow_without_new_sps(monkeypatch):
    monkeypatch.setattr(eufy_ws, "GOP_CACHE_MAX_CHUNKS", 3)
    stream = Livestream()
    stream.publish(SPS + PPS + IDR)
    for _ in range(5):
        stream.publish(P_FRAME)
    assert stream.gop == []
    stream.publish(IDR)  # camera does not repeat SPS/PPS
    stream.publish(P_FRAME)
    queue = stream.add_subscriber()
    assert [queue.get_nowait() for _ in range(queue.qsize())] == [SPS, PPS, IDR, P_FRAME]


def test_live_video_events_reach_a_late_ffmpeg_subscriber(bridge):
    async def scenario():
        bridge["bridge"] = fake = FakeBridge()
        client = EufyWsClient("ws://bridge", live_idle_stop_seconds=0)
        await client.ensure_connected()
        try:
            await client.start_livestream(SERIAL)
            for chunk in (SPS + PPS + IDR, P_FRAME):
                fake.push({
                    "type": "event",
                    "event": {
                        "source": "device",
                        "event": "livestream video data",
                        "serialNumber": SERIAL,
                        "buffer": {"type": "Buffer", "data": list(chunk)},
                        "metadata": {"videoCodec": 1},
                    },
                })
            for _ in range(50):
                await asyncio.sleep(0)
            queue = await client.subscribe(SERIAL)
            assert queue.get_nowait() == SPS + PPS + IDR
            assert queue.get_nowait() == P_FRAME
            assert len(fake.commands("device.start_livestream")) == 1
        finally:
            await client.close()

    run(scenario())


def test_unwatched_livestream_is_stopped_to_save_battery(bridge):
    async def scenario():
        bridge["bridge"] = fake = FakeBridge()
        client = EufyWsClient("ws://bridge", live_idle_stop_seconds=0.01)
        await client.ensure_connected()
        try:
            await client.start_livestream(SERIAL)
            await asyncio.sleep(0.1)
            assert [c["serialNumber"] for c in fake.commands("device.stop_livestream")] == [SERIAL]
        finally:
            await client.close()

    run(scenario())


def test_watched_livestream_is_not_idle_stopped(bridge):
    async def scenario():
        bridge["bridge"] = fake = FakeBridge()
        client = EufyWsClient("ws://bridge", live_idle_stop_seconds=0.01)
        await client.ensure_connected()
        try:
            await client.start_livestream(SERIAL)
            await client.subscribe(SERIAL)
            await asyncio.sleep(0.1)
            assert fake.commands("device.stop_livestream") == []
        finally:
            await client.close()

    run(scenario())


class FakeGo2rtc:
    """go2rtc ``/api/streams`` handler, modelled on upstream source.

    ``modern`` (>= 1.2.0, verified at v1.2.0 and v1.9.14
    ``internal/streams/api.go``): PATCH is memory-only (``streams.Patch``) and
    requires ``name``; PUT additionally writes go2rtc.yaml via
    ``app.PatchConfig``; ``GET ?src=<name>`` is 404 for an unknown stream.

    ``legacy`` (v1.1.x ``cmd/streams/streams.go``): the switch has no PATCH
    case, so PATCH falls through to the JSON dump -- HTTP 200, nothing
    registered -- and ``GET ?src=`` for an unknown stream encodes ``null``.
    """

    def __init__(self, legacy: bool = False) -> None:
        self.streams: dict[str, str] = {}
        self.requests: list[httpx.Request] = []
        self.persisted: dict[str, str] = {}
        self.legacy = legacy

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        name = request.url.params.get("name")
        src = request.url.params.get("src")
        if request.method == "PUT":
            self.streams[name or src] = src
            self.persisted[name or src] = src
            return httpx.Response(200)
        if request.method == "PATCH" and not self.legacy:
            if not name:
                return httpx.Response(400)
            self.streams[name] = src
            return httpx.Response(200)
        if request.method == "GET" and src and not self.legacy and src not in self.streams:
            return httpx.Response(404)
        payload = {"producers": [{"url": self.streams[src]}]} if src in self.streams else None
        return httpx.Response(200, json=payload)

    def restart(self) -> None:
        self.streams.clear()


@pytest.fixture
def go2rtc(monkeypatch):
    server = FakeGo2rtc()
    real_client = httpx.AsyncClient

    def make_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(server.handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(adapter.httpx, "AsyncClient", make_client)
    monkeypatch.setattr(adapter.settings, "go2rtc_url", "http://go2rtc:1984")
    monkeypatch.setattr(adapter.settings, "self_url", "http://adapter:8091")
    monkeypatch.setattr(adapter.settings, "stream_token", "stream-token")
    return server


def test_go2rtc_registration_uses_memory_only_patch(go2rtc):
    name = run(adapter._ensure_go2rtc_stream(SERIAL))
    assert name == f"eufy-{SERIAL}"
    patch, check = go2rtc.requests
    assert patch.method == "PATCH"
    assert patch.url.path == "/api/streams"
    assert dict(patch.url.params) == {
        "name": name,
        "src": f"ffmpeg:http://adapter:8091/internal/devices/{SERIAL}/h264?token=stream-token#video=copy",
    }
    assert (check.method, dict(check.url.params)) == ("GET", {"src": name})
    assert go2rtc.persisted == {}


def test_go2rtc_registration_survives_a_go2rtc_restart(go2rtc):
    name = run(adapter._ensure_go2rtc_stream(SERIAL))
    go2rtc.restart()
    run(adapter._ensure_go2rtc_stream(SERIAL))
    assert name in go2rtc.streams
    assert [r.method for r in go2rtc.requests] == ["PATCH", "GET", "PATCH", "GET"]


@pytest.mark.parametrize("status", [404, 405, 501])
def test_go2rtc_without_patch_fails_explicitly_and_never_puts(go2rtc, monkeypatch, status):
    def no_patch(request):
        go2rtc.requests.append(request)
        return httpx.Response(status)

    monkeypatch.setattr(go2rtc, "handler", no_patch)
    with pytest.raises(HTTPException) as excinfo:
        run(adapter._ensure_go2rtc_stream(SERIAL))
    assert excinfo.value.status_code == 502
    assert "1.2.0" in excinfo.value.detail
    assert [r.method for r in go2rtc.requests] == ["PATCH"]
    assert go2rtc.persisted == {}


def test_legacy_go2rtc_silent_patch_noop_is_detected_without_put(go2rtc):
    go2rtc.legacy = True
    with pytest.raises(HTTPException) as excinfo:
        run(adapter._ensure_go2rtc_stream(SERIAL))
    assert excinfo.value.status_code == 502
    assert "1.2.0" in excinfo.value.detail
    assert [r.method for r in go2rtc.requests] == ["PATCH", "GET"]
    assert go2rtc.streams == {} and go2rtc.persisted == {}


def test_go2rtc_rejected_source_is_reported(go2rtc, monkeypatch):
    monkeypatch.setattr(go2rtc, "handler", lambda request: httpx.Response(400, text="source not supported"))
    with pytest.raises(HTTPException) as excinfo:
        run(adapter._ensure_go2rtc_stream(SERIAL))
    assert excinfo.value.status_code == 502
    assert "HTTP 400" in excinfo.value.detail


def test_go2rtc_errors_never_echo_the_stream_token(go2rtc, monkeypatch):
    monkeypatch.setattr(go2rtc, "handler", lambda request: httpx.Response(500))
    with pytest.raises(HTTPException) as excinfo:
        run(adapter._ensure_go2rtc_stream(SERIAL))
    assert excinfo.value.status_code == 502
    assert "stream-token" not in str(excinfo.value.detail)
