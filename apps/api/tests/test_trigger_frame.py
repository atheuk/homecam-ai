"""The trigger frame must survive a session-starved NVR.

Regression test for observed production behaviour: the Dahua NVR sustains
only ~1-2 concurrent CGI sessions and was refusing roughly nine out of ten
snapshot requests. Ingestion would successfully detect on a frame, then the
analysis stage would throw that frame away, re-request one, lose the race,
and store the event with no photo at all.
"""
from __future__ import annotations

import pytest

from app.providers.base import ProviderUnavailableError
from app.services.ai_pipeline import _sample_frames


class _Provider:
    """Provider whose snapshots always fail, like a saturated NVR."""

    def __init__(self):
        self.calls = 0

    async def get_snapshot(self, camera_id: str) -> bytes:
        self.calls += 1
        raise ProviderUnavailableError("dahua")


class _WorkingProvider:
    def __init__(self):
        self.calls = 0

    async def get_snapshot(self, camera_id: str) -> bytes:
        self.calls += 1
        return f"frame-{self.calls}".encode()


@pytest.mark.asyncio
async def test_seed_frame_survives_a_fully_unavailable_provider():
    provider = _Provider()
    frames = await _sample_frames(provider, "dahua-channel-1", 3, seed=b"trigger")
    assert frames == [b"trigger"], "the frame we already had must not be discarded"


@pytest.mark.asyncio
async def test_without_a_seed_an_unavailable_provider_yields_nothing():
    """Guards the premise: this is the old behaviour that lost the photo."""
    provider = _Provider()
    assert await _sample_frames(provider, "dahua-channel-1", 3) == []


@pytest.mark.asyncio
async def test_seed_counts_toward_the_requested_sample_size():
    provider = _WorkingProvider()
    frames = await _sample_frames(provider, "dahua-channel-1", 3, seed=b"trigger")
    assert frames == [b"trigger", b"frame-1", b"frame-2"]
    assert provider.calls == 2, "seed should save exactly one NVR request"


@pytest.mark.asyncio
async def test_seed_is_first_so_it_wins_ties_in_best_photo_scoring():
    provider = _WorkingProvider()
    frames = await _sample_frames(provider, "dahua-channel-1", 2, seed=b"trigger")
    assert frames[0] == b"trigger"


@pytest.mark.asyncio
async def test_partial_topup_failure_keeps_the_frames_already_collected():
    class _FlakyProvider:
        def __init__(self):
            self.calls = 0

        async def get_snapshot(self, camera_id: str) -> bytes:
            self.calls += 1
            if self.calls > 1:
                raise ProviderUnavailableError("dahua")
            return b"frame-1"

    frames = await _sample_frames(_FlakyProvider(), "dahua-channel-1", 4, seed=b"trigger")
    assert frames == [b"trigger", b"frame-1"]


@pytest.mark.asyncio
async def test_no_seed_still_works_when_the_provider_is_healthy():
    provider = _WorkingProvider()
    frames = await _sample_frames(provider, "dahua-channel-1", 2)
    assert frames == [b"frame-1", b"frame-2"]
