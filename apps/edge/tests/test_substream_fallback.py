"""Sub-stream fallback when the NVR's snapshot buffer overflows.

Observed live on channel 1 (4K): snapshot.cgi returned HTTP 200 with
2,097,132 bytes ending 0x45A9 rather than the JPEG EOI 0xFFD9. The lost
tail decoded as a grey band over the bottom quarter of the frame, so the
near field - where someone at the door appears - was invisible to
detection. Nothing in the HTTP response indicates this; only the missing
marker does.
"""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from app import DahuaClient, EdgeSettings, _is_truncated_jpeg


def _jpeg(size: int = 1000) -> bytes:
    return b"\xff\xd8" + b"\x00" * (size - 4) + b"\xff\xd9"


def _truncated_jpeg(size: int = 1000) -> bytes:
    """A JPEG clipped mid-scan, exactly as this NVR emits it."""
    return b"\xff\xd8" + b"\x00" * (size - 4) + b"\x45\xa9"


def _settings(evidence_snapshot_timeout_seconds: float = 3.0) -> EdgeSettings:
    return EdgeSettings(
        dahua_host="192.0.2.1",
        dahua_username="u",
        dahua_password="p",
        evidence_snapshot_timeout_seconds=evidence_snapshot_timeout_seconds,
    )


def _client_recording(
    handler, *, evidence_snapshot_timeout_seconds: float = 3.0
) -> tuple[DahuaClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def _record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return (
        DahuaClient(
            settings=_settings(evidence_snapshot_timeout_seconds),
            transport=httpx.MockTransport(_record),
        ),
        seen,
    )


def test_truncation_detection_only_judges_real_jpegs():
    assert _is_truncated_jpeg(_truncated_jpeg()) is True
    assert _is_truncated_jpeg(_jpeg()) is False
    # Not a JPEG at all: must not be mistaken for damage.
    assert _is_truncated_jpeg(b"JPEGDATA") is False
    assert _is_truncated_jpeg(b"") is False


@pytest.mark.asyncio
async def test_complete_main_stream_frame_is_returned_untouched():
    client, seen = _client_recording(lambda r: httpx.Response(200, content=_jpeg()))
    assert await client.snapshot(1) == _jpeg()
    assert len(seen) == 1, "a healthy channel must cost exactly one request"
    assert seen[0].url.params["subtype"] == "0", "full resolution must be preserved"


@pytest.mark.asyncio
async def test_truncated_main_stream_falls_back_to_the_sub_stream():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("subtype") == "1":
            return httpx.Response(200, content=_jpeg(500))
        return httpx.Response(200, content=_truncated_jpeg())

    client, seen = _client_recording(handler)
    result = await client.snapshot(1)
    assert result == _jpeg(500), "must return the complete sub-stream frame"
    assert [r.url.params["subtype"] for r in seen] == ["0", "1"]


@pytest.mark.asyncio
async def test_channel_is_pinned_so_it_does_not_retry_the_main_stream_forever():
    """A session-limited NVR must not pay the overflow cost on every call."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("subtype") == "1":
            return httpx.Response(200, content=_jpeg(500))
        return httpx.Response(200, content=_truncated_jpeg())

    client, seen = _client_recording(handler)
    await client.snapshot(1)
    seen.clear()
    await client.snapshot(1)
    await client.snapshot(1)
    assert [r.url.params["subtype"] for r in seen] == ["1", "1"], "should go straight to sub stream"


@pytest.mark.asyncio
async def test_pinning_is_per_channel_and_does_not_punish_healthy_channels():
    def handler(request: httpx.Request) -> httpx.Response:
        channel = request.url.params.get("channel")
        if channel == "1" and request.url.params.get("subtype") == "0":
            return httpx.Response(200, content=_truncated_jpeg())
        return httpx.Response(200, content=_jpeg(500))

    client, seen = _client_recording(handler)
    await client.snapshot(1)
    seen.clear()
    await client.snapshot(2)
    assert [r.url.params["subtype"] for r in seen] == ["0"], "channel 2 keeps full resolution"


@pytest.mark.asyncio
async def test_a_partial_frame_is_still_better_than_no_frame():
    """If even the sub stream is truncated, return what we have."""
    client, _ = _client_recording(lambda r: httpx.Response(200, content=_truncated_jpeg()))
    result = await client.snapshot(1)
    assert result == _truncated_jpeg(), "an event should still get a photo"


@pytest.mark.asyncio
async def test_transport_failures_still_raise_rather_than_returning_garbage():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nvr refused the session")

    client, _ = _client_recording(handler)
    with pytest.raises(httpx.ConnectError):
        await client.snapshot(1)


@pytest.mark.asyncio
async def test_evidence_retries_main_stream_once_even_when_polling_is_pinned():
    first_main = True

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal first_main
        if request.url.params["subtype"] == "0" and first_main:
            first_main = False
            return httpx.Response(200, content=_truncated_jpeg())
        return httpx.Response(200, content=_jpeg(1500 if request.url.params["subtype"] == "0" else 500))

    client, seen = _client_recording(handler)
    assert await client.snapshot(1) == _jpeg(500)
    seen.clear()
    assert await client.evidence_snapshot(1) == _jpeg(1500)
    assert [r.url.params["subtype"] for r in seen] == ["0"]
    assert await client.snapshot(1) == _jpeg(500)


@pytest.mark.asyncio
async def test_truncated_evidence_is_rejected_without_substream_retry():
    client, seen = _client_recording(lambda r: httpx.Response(200, content=_truncated_jpeg()))
    with pytest.raises(ValueError, match="truncated"):
        await client.evidence_snapshot(1)
    assert [r.url.params["subtype"] for r in seen] == ["0"]


@pytest.mark.asyncio
async def test_evidence_deadline_includes_waiting_for_the_nvr_lock():
    client, seen = _client_recording(
        lambda r: httpx.Response(200, content=_jpeg()),
        evidence_snapshot_timeout_seconds=0.04,
    )
    started = time.monotonic()
    async with client._lock:
        with pytest.raises(httpx.TimeoutException, match="total deadline"):
            await client.evidence_snapshot(1)
    assert time.monotonic() - started < 0.2
    assert seen == [], "the evidence request must not start after its deadline"


@pytest.mark.asyncio
async def test_evidence_deadline_caps_slow_drip_response():
    class SlowDrip(httpx.AsyncByteStream):
        async def __aiter__(self):
            for part in (b"\xff\xd8", b"\x00", b"\x00", b"\x00", b"\x00", b"\xff\xd9"):
                await asyncio.sleep(0.02)
                yield part

    client, seen = _client_recording(
        lambda r: httpx.Response(200, stream=SlowDrip()),
        evidence_snapshot_timeout_seconds=0.07,
    )
    started = time.monotonic()
    with pytest.raises(httpx.TimeoutException, match="total deadline"):
        await client.evidence_snapshot(1)
    elapsed = time.monotonic() - started
    assert len(seen) == 1
    assert 0.05 <= elapsed < 0.2, "deadline must stop slow-drip bodies, not reset per chunk"
