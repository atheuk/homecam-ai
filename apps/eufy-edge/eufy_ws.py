"""Minimal client for the `eufy-security-ws` WebSocket bridge.

This module owns *all* knowledge of the eufy-security-ws protocol so the
HTTP layer in ``app.py`` stays a thin translation to HomeCam's adapter
contract. Nothing here ever sees a Eufy account credential: the bridge
process owns login, 2FA/captcha and session persistence, and we only talk
to it over the loopback/LAN socket it exposes.

Protocol summary:

* the server greets with ``{"type": "version", ...}``;
* the client pins a schema with ``set_api_schema``;
* ``start_listening`` returns the driver state. From schema 13 onwards its
  ``devices`` list holds *serial numbers only*, so name/type/battery/picture
  must be fetched per device with ``device.get_properties``;
* devices the bridge loads later (e.g. it was still logging in to Eufy when
  we connected) arrive as ``device added`` events;
* ``device.start_livestream`` begins a burst of ``livestream video data``
  events carrying raw H.264 Annex-B chunks as Node Buffer JSON. A late
  joiner gets no SPS/PPS until the next keyframe, so we cache the current
  GOP and replay it to new subscribers.

Upstream note: ``bropat/eufy-security-ws`` was archived in September 2026.
It still works, but it is a frozen dependency -- keep this module small so
re-pointing it at the successor SDK stays cheap.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

import websockets

logger = logging.getLogger("eufy-edge.ws")

# Schema we ask for. eufy-security-ws negotiates down to its own maximum,
# so this is an upper bound rather than a hard requirement.
PREFERRED_SCHEMA_VERSION = 21

# eufy device type codes that are doorbells. Anything else is reported to
# HomeCam as a plain camera; HomeCam only special-cases doorbell presses.
DOORBELL_TYPE_CODES = {5, 7, 16, 18, 19, 91, 93}

# How long to wait for the bridge to answer a command before giving up.
COMMAND_TIMEOUT_SECONDS = 20.0

# Upper bounds for the per-device "current GOP" replay cache. If a GOP grows
# past these (no keyframe for a long time) the cache is dropped until the
# next IDR rather than growing without bound.
GOP_CACHE_MAX_BYTES = 4 * 1024 * 1024
GOP_CACHE_MAX_CHUNKS = 300

# Stop a livestream that nobody subscribed to within this many seconds, so a
# ``/live`` call whose viewer never arrives cannot keep the battery doorbell
# awake indefinitely.
DEFAULT_LIVE_IDLE_STOP_SECONDS = 60.0

H264_NAL_IDR = 5
H264_NAL_SPS = 7
H264_NAL_PPS = 8
_START_CODE = b"\x00\x00\x01"


def _h264_nal_units(chunk: bytes) -> list[tuple[int, bytes]]:
    """``(type, payload)`` for each Annex-B NAL unit in a chunk.

    Payloads exclude start codes; the zero byte of a 4-byte start code that
    follows a NAL is trimmed from it.
    """
    units: list[tuple[int, bytes]] = []
    index = chunk.find(_START_CODE)
    while index != -1 and index + 3 < len(chunk):
        begin = index + 3
        following = chunk.find(_START_CODE, begin)
        end = len(chunk) if following == -1 else following
        payload = chunk[begin:end]
        if following != -1 and payload.endswith(b"\x00"):
            payload = payload[:-1]
        units.append((chunk[begin] & 0x1F, payload))
        index = following
    return units


def _h264_nal_types(chunk: bytes) -> set[int]:
    """NAL unit types present in an Annex-B H.264 chunk."""
    return {nal_type for nal_type, _ in _h264_nal_units(chunk)}


class EufyBridgeError(RuntimeError):
    """The bridge was reachable but refused or failed a command."""


class EufyBridgeUnavailable(RuntimeError):
    """The bridge could not be reached at all."""


def _decode_buffer(value: Any) -> bytes:
    """Decode the several shapes eufy-security-ws uses for binary data."""
    if isinstance(value, dict):
        if value.get("type") == "Buffer" and isinstance(value.get("data"), list):
            return bytes(value["data"])
        for key in ("data", "buffer"):
            if key in value:
                return _decode_buffer(value[key])
        return b""
    if isinstance(value, list):
        return bytes(value)
    if isinstance(value, str):
        # Property images arrive as base64, sometimes as a data: URL.
        payload = value.split(",", 1)[-1] if value.startswith("data:") else value
        try:
            return base64.b64decode(payload, validate=False)
        except (ValueError, TypeError):
            return b""
    return b""


@dataclass
class Livestream:
    """Fan-out buffer for one device's live H.264 byte stream."""

    queues: set[asyncio.Queue] = field(default_factory=set)
    started: bool = False
    # Chunks from the most recent IDR onwards, plus the latest SPS/PPS kept
    # separately (cameras may send parameter sets only once, at stream
    # start). go2rtc's ffmpeg always joins after the P2P stream has started
    # (``/live`` starts it, the HLS viewer arrives later); without SPS/PPS +
    # IDR ffmpeg can neither probe nor decode the feed, so new subscribers
    # are primed with ``SPS, PPS, GOP`` first.
    gop: list[bytes] = field(default_factory=list)
    gop_bytes: int = 0
    parameter_sets: dict[int, bytes] = field(default_factory=dict)
    idle_task: asyncio.Task | None = None
    # Subscribers that joined before any decodable GOP was cached (e.g. an
    # event clip recorder while a sleeping doorbell is still waking up). They
    # receive nothing until the next IDR, which is then prefixed with the
    # parameter sets, so ffmpeg never has to probe mid-GOP data.
    awaiting_keyframe: set[asyncio.Queue] = field(default_factory=set)
    keyframe: asyncio.Event = field(default_factory=asyncio.Event)
    closed: bool = False

    def _remember(self, chunk: bytes) -> set[int]:
        nal_types: set[int] = set()
        for nal_type, payload in _h264_nal_units(chunk):
            nal_types.add(nal_type)
            if nal_type in (H264_NAL_SPS, H264_NAL_PPS):
                self.parameter_sets[nal_type] = payload
        if H264_NAL_IDR in nal_types:
            self.gop = [chunk]
            self.gop_bytes = len(chunk)
            self.keyframe.set()
            return nal_types
        if not self.gop:
            # No keyframe yet (or dropped after an overflow): a GOP that does
            # not start at an IDR cannot be decoded, so wait for the next one.
            return nal_types
        self.gop.append(chunk)
        self.gop_bytes += len(chunk)
        if self.gop_bytes > GOP_CACHE_MAX_BYTES or len(self.gop) > GOP_CACHE_MAX_CHUNKS:
            self.gop = []
            self.gop_bytes = 0
        return nal_types

    def _parameter_prefix(self, present: set[int]) -> list[bytes]:
        return [
            b"\x00\x00\x00\x01" + self.parameter_sets[nal_type]
            for nal_type in (H264_NAL_SPS, H264_NAL_PPS)
            if nal_type in self.parameter_sets and nal_type not in present
        ]

    def _primer(self) -> list[bytes]:
        if not self.gop:
            return []
        return self._parameter_prefix(_h264_nal_types(self.gop[0])) + self.gop

    def add_subscriber(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        primer = self._primer()
        for chunk in primer:
            queue.put_nowait(chunk)
        if not primer:
            self.awaiting_keyframe.add(queue)
        self.queues.add(queue)
        return queue

    def remove_subscriber(self, queue: asyncio.Queue) -> None:
        self.queues.discard(queue)
        self.awaiting_keyframe.discard(queue)

    async def wait_for_keyframe(self, timeout: float) -> bool:
        """Wait (bounded) until a decodable GOP start has arrived.

        Returns False on timeout or as soon as the stream is closed (bridge
        reported the livestream stopped, socket dropped, device removed).
        """
        try:
            await asyncio.wait_for(self.keyframe.wait(), timeout)
        except asyncio.TimeoutError:
            return False
        return not self.closed

    def publish(self, chunk: bytes) -> None:
        nal_types = self._remember(chunk)
        for queue in list(self.queues):
            # Drop rather than block: a slow reader must never stall the
            # socket that every other device's events share.
            if queue.qsize() > 512:
                continue
            if queue in self.awaiting_keyframe:
                if H264_NAL_IDR not in nal_types:
                    continue
                self.awaiting_keyframe.discard(queue)
                for prefix in self._parameter_prefix(nal_types):
                    queue.put_nowait(prefix)
            queue.put_nowait(chunk)

    def close(self) -> None:
        if self.idle_task is not None and self.idle_task is not asyncio.current_task():
            self.idle_task.cancel()
        self.idle_task = None
        for queue in list(self.queues):
            queue.put_nowait(None)
        self.queues.clear()
        self.awaiting_keyframe.clear()
        # Wake anyone waiting for a keyframe; ``closed`` makes them fail fast.
        self.closed = True
        self.keyframe.set()
        self.gop = []
        self.gop_bytes = 0
        self.parameter_sets = {}


# eufy-security-ws device events (``state`` true on start) and the matching
# boolean properties, mapped onto HomeCam trigger names.
TRIGGER_EVENTS = {
    "motion detected": "motion",
    "person detected": "person",
    "stranger person detected": "person",
    "pet detected": "animal",
    "dog detected": "animal",
    "vehicle detected": "vehicle",
    "rings": "doorbell",
}
TRIGGER_PROPERTIES = {
    "motionDetected": "motion",
    "personDetected": "person",
    "petDetected": "animal",
    "ringing": "doorbell",
}


class EufyWsClient:
    """Maintains one persistent connection to eufy-security-ws."""

    def __init__(
        self,
        url: str,
        connect_timeout: float = 10.0,
        live_idle_stop_seconds: float = DEFAULT_LIVE_IDLE_STOP_SECONDS,
    ) -> None:
        self._url = url
        self._connect_timeout = connect_timeout
        self._live_idle_stop_seconds = live_idle_stop_seconds
        self._background: set[asyncio.Task] = set()
        self._ws: Any | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._devices: dict[str, dict] = {}
        self._properties: dict[str, dict] = {}
        self._livestreams: dict[str, Livestream] = {}
        self._reader_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._driver_connected = False
        self._captcha_pending = False
        self._mfa_pending = False
        # Sync callbacks ``(serial, trigger)`` fired on motion/person/ring
        # starts; used by the bounded event clip recorder.
        self.trigger_listeners: list = []

    # ---------------------------------------------------------------- state

    @property
    def auth_state(self) -> str:
        """Map bridge state onto the vocabulary HomeCam's provider expects."""
        if self._captcha_pending:
            return "captcha_required"
        if self._mfa_pending:
            return "2fa_required"
        if self._ws is None:
            return "unauthenticated"
        if self._driver_connected:
            return "authenticated"
        return "unknown"

    def devices(self) -> list[dict]:
        return [self._normalise(serial) for serial in sorted(self._devices)]

    def has_device(self, serial: str) -> bool:
        return serial in self._devices

    # ----------------------------------------------------------- connection

    async def ensure_connected(self) -> None:
        # ``self._ws`` is cleared by the reader task when the socket dies, so
        # a non-None value is a sufficient liveness check across the
        # websockets versions that renamed ``.open``/``.closed``/``.state``.
        if self._ws is not None:
            return
        async with self._lock:
            if self._ws is not None:
                return
            await self._connect()

    async def _connect(self) -> None:
        try:
            self._ws = await asyncio.wait_for(
                websockets.connect(self._url, max_size=None, ping_interval=20),
                timeout=self._connect_timeout,
            )
        except (OSError, asyncio.TimeoutError, websockets.WebSocketException) as exc:
            self._ws = None
            raise EufyBridgeUnavailable(f"cannot reach eufy-security-ws at {self._url}: {exc}") from exc

        self._reader_task = asyncio.create_task(self._read_loop())
        try:
            await self._send_command({"command": "set_api_schema", "schemaVersion": PREFERRED_SCHEMA_VERSION})
            state = await self._send_command({"command": "start_listening"})
        except Exception:
            await self.close()
            raise
        self._ingest_state(state)
        # Schema >= 13 only lists serials; without this every device would
        # appear as a nameless "camera" with no doorbell/snapshot support.
        for serial in list(self._devices):
            await self._load_properties(serial)
        logger.info("connected to eufy-security-ws; %d device(s) known", len(self._devices))

    async def _load_properties(self, serial: str) -> None:
        """Best-effort property fetch that bypasses the connect lock."""
        try:
            result = await self._send_command({"command": "device.get_properties", "serialNumber": serial})
        except (EufyBridgeError, EufyBridgeUnavailable) as exc:
            logger.warning("could not load properties for a Eufy device: %s", exc)
            return
        self._merge_properties(serial, result)

    def _merge_properties(self, serial: str, result: dict) -> None:
        properties = result.get("properties") if isinstance(result.get("properties"), dict) else result
        if isinstance(properties, dict):
            self._properties.setdefault(serial, {}).update(properties)

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def close(self) -> None:
        for stream in self._livestreams.values():
            stream.close()
        self._livestreams.clear()
        for pending in list(self._background):
            pending.cancel()
        self._background.clear()
        task, self._reader_task = self._reader_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None
        self._driver_connected = False

    # -------------------------------------------------------------- reading

    async def _read_loop(self) -> None:
        ws = self._ws
        assert ws is not None
        try:
            async for raw in ws:
                try:
                    message = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - connection died; surface via auth_state
            logger.warning("eufy-security-ws connection lost: %s", exc)
        finally:
            self._fail_pending(EufyBridgeUnavailable("eufy-security-ws connection closed"))
            for stream in self._livestreams.values():
                stream.close()
            self._livestreams.clear()
            self._driver_connected = False
            self._ws = None

    def _fail_pending(self, error: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()

    def _dispatch(self, message: dict) -> None:
        kind = message.get("type")
        if kind == "result":
            future = self._pending.pop(message.get("messageId", ""), None)
            if future is None or future.done():
                return
            if message.get("success"):
                future.set_result(message.get("result") or {})
            else:
                future.set_exception(EufyBridgeError(str(message.get("errorCode") or "command failed")))
            return
        if kind == "event":
            self._handle_event(message.get("event") or {})

    def _handle_event(self, event: dict) -> None:
        name = event.get("event")
        serial = event.get("serialNumber")

        if name == "livestream video data" and serial:
            stream = self._livestreams.get(serial)
            if stream is not None:
                chunk = _decode_buffer(event.get("buffer"))
                if chunk:
                    stream.publish(chunk)
            return

        if name == "livestream stopped" and serial:
            stream = self._livestreams.pop(serial, None)
            if stream is not None:
                stream.close()
            return

        if name in TRIGGER_EVENTS and serial:
            if event.get("state", True):
                self._fire_trigger(serial, TRIGGER_EVENTS[name])
            return

        if name == "property changed" and serial:
            prop = str(event.get("name"))
            previous = self._properties.setdefault(serial, {}).get(prop)
            self._properties[serial][prop] = event.get("value")
            if prop in TRIGGER_PROPERTIES and event.get("value") is True and previous is not True:
                self._fire_trigger(serial, TRIGGER_PROPERTIES[prop])
            return

        if name == "device added":
            # Devices load after the bridge finishes its Eufy cloud login,
            # which is often *after* we connected and got an empty list.
            added = event.get("device")
            added_serial = added if isinstance(added, str) else (added or {}).get("serialNumber")
            if isinstance(added_serial, str) and added_serial:
                self._devices.setdefault(added_serial, {"serialNumber": added_serial})
                if isinstance(added, dict):
                    self._devices[added_serial] = added
                    self._properties.setdefault(added_serial, {}).update(added)
                else:
                    self._spawn(self._load_properties(added_serial))
            return

        if name == "device removed":
            removed = event.get("device")
            removed_serial = removed if isinstance(removed, str) else (removed or {}).get("serialNumber")
            if isinstance(removed_serial, str):
                self._devices.pop(removed_serial, None)
                self._properties.pop(removed_serial, None)
                stream = self._livestreams.pop(removed_serial, None)
                if stream is not None:
                    stream.close()
            return

        if name == "captcha request":
            self._captcha_pending = True
            return
        if name == "verify code":
            self._mfa_pending = True
            return
        if name == "connected":
            self._driver_connected = True
            self._captcha_pending = False
            self._mfa_pending = False
            return
        if name == "disconnected":
            self._driver_connected = False

    def _fire_trigger(self, serial: str, trigger: str) -> None:
        for listener in list(self.trigger_listeners):
            try:
                listener(serial, trigger)
            except Exception:  # noqa: BLE001 - a listener must never break the reader
                logger.exception("event trigger listener failed")

    def _ingest_state(self, result: dict) -> None:
        state = result.get("state") or {}
        driver = state.get("driver") or {}
        self._driver_connected = bool(driver.get("connected", True))
        for device in state.get("devices") or []:
            if isinstance(device, str):
                # Older schemas return serials only; properties arrive later.
                self._devices.setdefault(device, {"serialNumber": device})
                continue
            serial = device.get("serialNumber")
            if serial:
                self._devices[serial] = device
                self._properties.setdefault(serial, {}).update(device)

    # ------------------------------------------------------------- commands

    async def _send_command(self, payload: dict) -> dict:
        ws = self._ws
        if ws is None:
            raise EufyBridgeUnavailable("not connected to eufy-security-ws")
        message_id = uuid.uuid4().hex
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        try:
            await ws.send(json.dumps({**payload, "messageId": message_id}))
            return await asyncio.wait_for(future, timeout=COMMAND_TIMEOUT_SECONDS)
        except asyncio.TimeoutError as exc:
            raise EufyBridgeError(f"timed out waiting for '{payload.get('command')}'") from exc
        finally:
            self._pending.pop(message_id, None)

    async def command(self, payload: dict) -> dict:
        await self.ensure_connected()
        return await self._send_command(payload)

    async def refresh_properties(self, serial: str) -> dict:
        try:
            result = await self.command({"command": "device.get_properties", "serialNumber": serial})
        except (EufyBridgeError, EufyBridgeUnavailable):
            return self._properties.get(serial, {})
        self._merge_properties(serial, result)
        return self._properties.get(serial, {})

    # ------------------------------------------------------------ normalise

    def _normalise(self, serial: str) -> dict:
        raw = {**self._devices.get(serial, {}), **self._properties.get(serial, {})}
        type_code = raw.get("type")
        is_doorbell = isinstance(type_code, int) and type_code in DOORBELL_TYPE_CODES
        if not is_doorbell:
            haystack = f"{raw.get('model', '')} {raw.get('name', '')}".lower()
            is_doorbell = "doorbell" in haystack
        battery = raw.get("battery")
        return {
            "id": serial,
            "name": str(raw.get("name") or f"Eufy {serial}"),
            "type": "doorbell" if is_doorbell else "camera",
            "model": str(raw.get("model") or "Eufy device"),
            "online": bool(raw.get("enabled", True)),
            "battery_level": int(battery) if isinstance(battery, (int, float)) else None,
            "capabilities": {
                # Only claim what this bridge has actually demonstrated. The
                # provider maps anything falsy to UNAVAILABLE/UNKNOWN rather
                # than inventing support.
                "snapshot": bool(self._picture_bytes(serial)) or bool(raw.get("pictureUrl")),
                "eventImages": True,
                "liveStream": True,
                "motionEvents": "motionDetected" in raw,
                "personEvents": "personDetected" in raw,
                "doorbellEvents": is_doorbell,
                "battery": battery is not None,
            },
        }

    def _picture_bytes(self, serial: str) -> bytes:
        picture = self._properties.get(serial, {}).get("picture")
        return _decode_buffer(picture) if picture else b""

    # ------------------------------------------------------------ snapshots

    async def snapshot(self, serial: str) -> bytes:
        """Return the most recent event image for a device.

        Deliberately does *not* wake the device: the T8210 is battery
        powered and a P2P wake per snapshot poll would flatten it. The
        bridge keeps the last push image, which is what the doorbell's own
        notifications show.
        """
        await self.ensure_connected()
        image = self._picture_bytes(serial)
        if image:
            return image
        await self.refresh_properties(serial)
        image = self._picture_bytes(serial)
        if image:
            return image
        raise EufyBridgeError("no event image available yet for this device")

    # ----------------------------------------------------------- livestream

    async def start_livestream(self, serial: str) -> Livestream:
        await self.ensure_connected()
        stream = self._livestreams.get(serial)
        if stream is None:
            stream = Livestream()
            self._livestreams[serial] = stream
        if not stream.started:
            try:
                await self.command({"command": "device.start_livestream", "serialNumber": serial})
            except EufyBridgeError as exc:
                # "already streaming" is a success for our purposes.
                if "already" not in str(exc).lower():
                    self._livestreams.pop(serial, None)
                    raise
            stream.started = True
            if not stream.queues and self._live_idle_stop_seconds > 0:
                stream.idle_task = asyncio.get_running_loop().create_task(self._stop_if_idle(serial, stream))
        return stream

    async def _stop_if_idle(self, serial: str, stream: Livestream) -> None:
        await asyncio.sleep(self._live_idle_stop_seconds)
        if self._livestreams.get(serial) is stream and not stream.queues:
            logger.info("stopping an unwatched Eufy livestream")
            await self.stop_livestream(serial)

    async def stop_livestream(self, serial: str, expected: Livestream | None = None) -> None:
        if expected is not None and self._livestreams.get(serial) is not expected:
            # The caller's stream already ended and may have been replaced by
            # someone else's (e.g. a live viewer): never stop that one.
            return
        stream = self._livestreams.pop(serial, None)
        if stream is not None:
            stream.close()
        with contextlib.suppress(EufyBridgeError, EufyBridgeUnavailable):
            await self.command({"command": "device.stop_livestream", "serialNumber": serial})

    async def subscribe(self, serial: str) -> asyncio.Queue:
        stream = await self.start_livestream(serial)
        return stream.add_subscriber()

    async def unsubscribe(self, serial: str, queue: asyncio.Queue) -> None:
        stream = self._livestreams.get(serial)
        if stream is None:
            return
        stream.remove_subscriber(queue)
        if not stream.queues:
            # Last viewer left: stop P2P so the battery is not drained by a
            # stream nobody is watching.
            await self.stop_livestream(serial)
