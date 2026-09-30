"""Zero-detection watchdog and detector status surface (incident 2026-09-30).

The production blackout was invisible because every signal that existed
answered a different question: ingestion said frames were arriving, the
providers said the cameras were online, and "zero events" is also what a
quiet driveway looks like. These tests pin the two signals that do
distinguish blind from quiet.
"""
import logging

import pytest

from app.ai.detector import resolve_detector
from app.config import settings
from app.services import detector_watchdog


@pytest.fixture(autouse=True)
def _clean_watchdog():
    detector_watchdog.reset()
    yield
    detector_watchdog.reset()


@pytest.fixture
def fast_watchdog(monkeypatch):
    monkeypatch.setattr(settings, "detector_blackout_window_seconds", 60.0)
    monkeypatch.setattr(settings, "detector_blackout_min_frames", 10)


def _feed(frames, detections_each=0, *, start=0.0, step=1.0):
    for index in range(frames):
        detector_watchdog.record_frame(detections_each, now=start + index * step)


def test_watchdog_fires_when_frames_succeed_but_nothing_is_ever_detected(fast_watchdog, caplog):
    with caplog.at_level(logging.ERROR):
        _feed(90, detections_each=0)

    assert detector_watchdog.in_blackout() is True
    assert "DETECTOR BLACKOUT" in caplog.text
    assert detector_watchdog.status()["blackout"] is True


def test_watchdog_does_not_fire_during_a_quiet_but_working_period(fast_watchdog, caplog):
    """A working detector on a real scene keeps returning boxes (a parked
    car, a tree) even when every one of them is suppressed and no event is
    emitted. That must never be reported as a blackout."""
    with caplog.at_level(logging.ERROR):
        for index in range(90):
            # One box every tenth frame: far too quiet to emit events, but
            # unambiguous proof the detector can see.
            detector_watchdog.record_frame(1 if index % 10 == 0 else 0, now=float(index))

    assert detector_watchdog.in_blackout() is False
    assert "DETECTOR BLACKOUT" not in caplog.text


def test_watchdog_needs_enough_frames_before_silence_is_evidence(fast_watchdog):
    """Three frames in an hour is an acquisition problem, not a blackout."""
    _feed(5, detections_each=0, step=100.0)
    assert detector_watchdog.in_blackout() is False


def test_watchdog_clears_when_detections_resume(fast_watchdog, caplog):
    _feed(90, detections_each=0)
    assert detector_watchdog.in_blackout() is True

    with caplog.at_level(logging.WARNING):
        detector_watchdog.record_frame(2, now=1000.0)

    assert detector_watchdog.in_blackout() is False
    assert "BLACKOUT CLEARED" in caplog.text
    status = detector_watchdog.status(now=1000.0)
    assert status["seconds_since_last_detection"] == 0.0


def test_watchdog_can_be_disabled(monkeypatch, fast_watchdog):
    monkeypatch.setattr(settings, "detector_watchdog_enabled", False)
    _feed(90, detections_each=0)
    assert detector_watchdog.in_blackout() is False


async def test_status_endpoint_reports_a_healthy_detector(client):
    response = await client.get("/api/v1/system/status")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["detector"]["active_backend"] == "mock"
    assert body["detector"]["is_intended_backend"] is True
    assert set(body["supported_backends"]) == {"mock", "opencv", "onnx", "rtdetr"}


async def test_status_endpoint_returns_503_when_the_detector_is_not_the_intended_one(
    client, monkeypatch
):
    """The exact production configuration: a backend name the code does not
    accept. A probe must be able to see it without reading logs."""
    from app.ai import detector as detector_module

    degraded = resolve_detector("rtdetr-r50", model_path="/app/models/rtdetr-r50.onnx")
    monkeypatch.setattr(detector_module, "_active_detector", degraded.detector)
    monkeypatch.setattr(detector_module, "_active_status", degraded.status)

    response = await client.get("/api/v1/system/status")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "blind"
    assert body["detecting"] is False
    assert body["detector"]["degraded"] is True
    assert "/app/models/rtdetr-r50.onnx" in body["reasons"][0]

    ready = await client.get("/ready")
    # Readiness stays 200 on purpose - see app/main.py - but says degraded.
    assert ready.status_code == 200
    assert ready.json()["status"] == "degraded"
    assert ready.json()["detector"]["degraded"] is True


async def test_status_endpoint_returns_503_during_a_blackout(client, fast_watchdog):
    _feed(90, detections_each=0)
    response = await client.get("/api/v1/system/status")
    assert response.status_code == 503
    assert response.json()["detector_watchdog"]["blackout"] is True
    assert "zero detections" in response.json()["reasons"][0]
