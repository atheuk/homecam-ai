"""Minimal client for the `eufy-security-ws` WebSocket bridge.

This module owns *all* knowledge of the eufy-security-ws protocol so the
HTTP layer in ``app.py`` stays a thin translation to HomeCam's adapter
contract. Nothing here ever sees a Eufy account credential: the bridge
process owns login, 2FA/captcha and session persistence, and we only talk
to it over the loopback/LAN socket it exposes.

Protocol summary:

* the server greets with ``{"type": "version", ...}``;
* the client pins a schema with ``set_api_schema``;
* ``start_listening`` returns the full driver state, including devices;
* ``device.start_livestream`` begins a burst of ``livestream video data``
  events carrying raw H.264 Annex-B chunks as Node Buffer JSON.

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

    def publish(self, chunk: bytes) -> None:
        for queue in list(self.queues):
            # Drop rather than block: a slow reader must never stall the
            # socket that every other device's events share.
            if queue.qsize() > 512:
                continue
            queue.put_nowait(chunk)

    def close(self) -> None:
        for queue in list(self.queues):
            queue.put_nowait(None)
        self.queues.clear()


class EufyWsClient:
    """Maintains one persistent connection to eufy-security-ws."""

    def __init__(self, url: str, connect_timeout: float = 10.0) -> None:
        self._url = url
        self._connect_timeout = connect_timeout
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
        logger.info("connected to eufy-security-ws; %d device(s) known", len(self._devices))

    async def close(self) -> None:
        for stream in self._livestreams.values():
            stream.close()
        self._livestreams.clear()
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

        if name == "property changed" and serial:
            self._properties.setdefault(serial, {})[str(event.get("name"))] = event.get("value")
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
        properties = result.get("properties") or result
        if isinstance(properties, dict):
            self._properties.setdefault(serial, {}).update(properties)
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
        return stream

    async def stop_livestream(self, serial: str) -> None:
        stream = self._livestreams.pop(serial, None)
        if stream is not None:
            stream.close()
        with contextlib.suppress(EufyBridgeError, EufyBridgeUnavailable):
            await self.command({"command": "device.stop_livestream", "serialNumber": serial})

    async def subscribe(self, serial: str) -> asyncio.Queue:
        stream = await self.start_livestream(serial)
        queue: asyncio.Queue = asyncio.Queue()
        stream.queues.add(queue)
        return queue

    async def unsubscribe(self, serial: str, queue: asyncio.Queue) -> None:
        stream = self._livestreams.get(serial)
        if stream is None:
            return
        stream.queues.discard(queue)
        if not stream.queues:
            # Last viewer left: stop P2P so the battery is not drained by a
            # stream nobody is watching.
            await self.stop_livestream(serial)
