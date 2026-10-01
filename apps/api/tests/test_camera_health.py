"""Deterministic camera-health watchdog: obstruction/frozen-feed/offline
detection (SPEC follow-up).

Uses real PIL-encoded frames (not the mock provider's placeholder bytes) so
the actual pixel-stat heuristics in ``app.ai.camera_health`` run for real,
per SPEC's "mock cannot see pixels" note.
"""
import io

import pytest
from PIL import Image
from sqlalchemy import delete

from app.ai import camera_health
from app.db import SessionLocal
from app.models.db import Incident


def _jpeg(color: tuple[int, int, int], noise: int = 0) -> bytes:
    """A small solid-color (optionally noisy) JPEG frame."""
    image = Image.new("RGB", (64, 64), color=color)
    if noise:
        import random

        pixels = image.load()
        rng = random.Random(42)
        for x in range(image.width):
            for y in range(image.height):
                r, g, b = pixels[x, y]
                jitter = rng.randint(-noise, noise)
                pixels[x, y] = (
                    max(0, min(255, r + jitter)),
                    max(0, min(255, g + jitter)),
                    max(0, min(255, b + jitter)),
                )
    buf = io.BytesIO()
    image.save(buf, format="JPEG")
    return buf.getvalue()


FLAT_FRAME = _jpeg((120, 120, 120))  # near-zero stddev: looks obstructed
NOISY_FRAME_A = _jpeg((120, 120, 120), noise=80)
NOISY_FRAME_B = _jpeg((60, 160, 90), noise=80)


@pytest.fixture(autouse=True)
async def _clean_state(client):
    # Depending on the ``client`` fixture (unused directly) ensures the app
    # lifespan has run and the DB schema exists before we touch it.
    camera_health.reset()

    async def _clear():
        async with SessionLocal() as session:
            await session.execute(delete(Incident))
            await session.commit()

    await _clear()
    yield
    camera_health.reset()
    await _clear()


def _open_incidents(rows: list[Incident], kind: str) -> list[Incident]:
    return [r for r in rows if r.kind == kind and r.status == "open"]


async def _all_incidents() -> list[Incident]:
    async with SessionLocal() as session:
        from sqlalchemy import select

        result = await session.execute(select(Incident))
        return list(result.scalars().all())


@pytest.mark.asyncio
async def test_online_to_offline_transition_raises_incident_then_resolves():
    await camera_health.evaluate_online_state(SessionLocal, "cam-1", "Cam One", True)
    assert _open_incidents(await _all_incidents(), "camera_offline") == []

    await camera_health.evaluate_online_state(SessionLocal, "cam-1", "Cam One", False)
    open_now = _open_incidents(await _all_incidents(), "camera_offline")
    assert len(open_now) == 1
    assert open_now[0].camera_id == "cam-1"

    await camera_health.evaluate_online_state(SessionLocal, "cam-1", "Cam One", True)
    assert _open_incidents(await _all_incidents(), "camera_offline") == []


@pytest.mark.asyncio
async def test_first_observation_never_raises_a_spurious_transition():
    # No prior known state -> must not treat "first seen as offline" as a
    # transition (there is nothing to compare against yet).
    await camera_health.evaluate_online_state(SessionLocal, "cam-fresh", "Cam Fresh", False)
    assert _open_incidents(await _all_incidents(), "camera_offline") == []


@pytest.mark.asyncio
async def test_seed_from_open_incidents_lets_a_still_open_offline_incident_auto_resolve():
    """Regression test: without seeding, a process restart while a camera is
    genuinely offline would reset watchdog state to unknown, and the next
    "camera is back" observation would be swallowed by the first-observation
    guard - leaving the incident stuck open forever even after the camera
    recovers. Seeding from the DB's already-open incident must let the
    online transition be detected and the incident auto-resolve."""
    # Raise an offline incident, then simulate a process restart by clearing
    # in-memory state without touching the DB row it created.
    await camera_health.evaluate_online_state(SessionLocal, "cam-1", "Cam One", True)
    await camera_health.evaluate_online_state(SessionLocal, "cam-1", "Cam One", False)
    assert len(_open_incidents(await _all_incidents(), "camera_offline")) == 1
    camera_health.reset()

    await camera_health.seed_from_open_incidents(SessionLocal)
    # Seeding alone must not raise anything new.
    assert len(_open_incidents(await _all_incidents(), "camera_offline")) == 1

    await camera_health.evaluate_online_state(SessionLocal, "cam-1", "Cam One", True)
    assert _open_incidents(await _all_incidents(), "camera_offline") == []


@pytest.mark.asyncio
async def test_seed_from_open_incidents_does_not_affect_cameras_without_open_incidents():
    await camera_health.seed_from_open_incidents(SessionLocal)
    # A camera never seen before still gets the "first observation" pass,
    # i.e. seeding a healthy DB must not spuriously mark it as last-offline.
    await camera_health.evaluate_online_state(SessionLocal, "cam-new", "Cam New", False)
    assert _open_incidents(await _all_incidents(), "camera_offline") == []


@pytest.mark.asyncio
async def test_sustained_flat_frames_raise_obstruction_incident(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "camera_obstruction_confirm_samples", 3)
    for _ in range(2):
        await camera_health.evaluate_frame(SessionLocal, "cam-2", "Cam Two", FLAT_FRAME)
    assert _open_incidents(await _all_incidents(), "camera_obstruction") == []

    await camera_health.evaluate_frame(SessionLocal, "cam-2", "Cam Two", FLAT_FRAME)
    open_now = _open_incidents(await _all_incidents(), "camera_obstruction")
    assert len(open_now) == 1

    # A subsequent high-detail frame clears the obstruction.
    await camera_health.evaluate_frame(SessionLocal, "cam-2", "Cam Two", NOISY_FRAME_A)
    assert _open_incidents(await _all_incidents(), "camera_obstruction") == []


@pytest.mark.asyncio
async def test_non_decodable_frame_bytes_are_a_safe_noop():
    # Mock-provider style placeholder bytes: not a real image at all.
    await camera_health.evaluate_frame(SessionLocal, "cam-mock", "Cam Mock", b"not-an-image")
    assert await _all_incidents() == []


@pytest.mark.asyncio
async def test_unchanged_frame_stats_sustained_past_threshold_raises_frozen(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "camera_frozen_seconds", 0.05)
    monkeypatch.setattr(settings, "camera_obstruction_confirm_samples", 1000)  # isolate frozen-only

    await camera_health.evaluate_frame(SessionLocal, "cam-3", "Cam Three", NOISY_FRAME_A)
    assert _open_incidents(await _all_incidents(), "camera_frozen") == []

    import asyncio

    # Second identical frame: this is where "frozen_since" starts ticking.
    await camera_health.evaluate_frame(SessionLocal, "cam-3", "Cam Three", NOISY_FRAME_A)
    assert _open_incidents(await _all_incidents(), "camera_frozen") == []

    await asyncio.sleep(0.1)
    await camera_health.evaluate_frame(SessionLocal, "cam-3", "Cam Three", NOISY_FRAME_A)
    open_now = _open_incidents(await _all_incidents(), "camera_frozen")
    assert len(open_now) == 1

    # A materially different frame resolves it.
    await camera_health.evaluate_frame(SessionLocal, "cam-3", "Cam Three", NOISY_FRAME_B)
    assert _open_incidents(await _all_incidents(), "camera_frozen") == []


@pytest.mark.asyncio
async def test_changing_frames_never_raise_frozen_incident(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "camera_frozen_seconds", 0.01)
    monkeypatch.setattr(settings, "camera_obstruction_confirm_samples", 1000)

    import asyncio

    for frame in (NOISY_FRAME_A, NOISY_FRAME_B, NOISY_FRAME_A, NOISY_FRAME_B):
        await camera_health.evaluate_frame(SessionLocal, "cam-4", "Cam Four", frame)
        await asyncio.sleep(0.02)

    assert _open_incidents(await _all_incidents(), "camera_frozen") == []
