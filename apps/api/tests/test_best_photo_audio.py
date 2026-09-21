"""Best-photo selection, AI provider output shape, and audio VAD."""
import math
import struct

import pytest

from app.ai.audio import DEFAULT_ENERGY_THRESHOLD, analyze_pcm
from app.ai.best_photo import score_frame, select_best_photo, sharpness_score
from app.ai.detector import BoundingBox, Detection, DetectionContext, MockDetector
from app.ai.provider import AnalysisContext, MockAIProvider
from app.ai.schemas import GroundingError, ImageAnalysis, assert_grounded

PERSON = Detection("person", 0.9, BoundingBox(0.2, 0.2, 0.5, 0.8))
CAR = Detection("car", 0.7, BoundingBox(0.5, 0.5, 0.9, 0.9))


class _ScriptedDetector:
    name = "scripted"

    def __init__(self, by_frame):
        self._by_frame = by_frame

    def detect(self, image, context):
        return list(self._by_frame.get(image, []))


def test_sharpness_is_deterministic_and_bounded():
    frame = bytes(range(256)) * 4
    first = sharpness_score(frame)
    assert first == sharpness_score(frame)
    assert 0.0 <= first <= 1.0


def test_flat_frame_is_less_sharp_than_noisy_frame():
    flat = bytes([128]) * 1024
    noisy = bytes((i * 61) % 256 for i in range(1024))
    assert sharpness_score(flat) < sharpness_score(noisy)


def test_score_frame_prefers_the_target_class():
    score, sharpness, detection = score_frame(b"\x00\xff" * 64, [CAR, PERSON], {"person"})
    assert detection is PERSON
    assert 0.0 <= score <= 1.0
    assert 0.0 <= sharpness <= 1.0


def test_score_frame_falls_back_to_any_detection():
    _, _, detection = score_frame(b"\x00\xff" * 64, [CAR], {"person"})
    assert detection is CAR


def test_score_frame_without_detections_scores_only_sharpness():
    score, sharpness, detection = score_frame(b"\x00\xff" * 64, [], {"person"})
    assert detection is None
    assert score == pytest.approx(0.4 * sharpness)


def test_select_best_photo_picks_the_most_confident_sharp_frame():
    blurry = bytes([128]) * 512
    sharp = bytes((i * 61) % 256 for i in range(512))
    detector = _ScriptedDetector(
        {
            blurry: [Detection("person", 0.95, PERSON.bbox)],
            sharp: [Detection("person", 0.9, PERSON.bbox)],
        }
    )
    best = select_best_photo(
        [blurry, sharp], detector, DetectionContext(camera_id="c"), {"person"}, crop=False
    )
    assert best is not None
    assert best.frame_index == 1  # sharper frame wins despite slightly lower confidence
    assert best.detection.label == "person"


def test_select_best_photo_returns_none_without_frames():
    best = select_best_photo([], MockDetector(), DetectionContext(camera_id="c"), {"person"})
    assert best is None


def test_select_best_photo_is_serialisable():
    frame = bytes((i * 31) % 256 for i in range(256))
    detector = _ScriptedDetector({frame: [PERSON]})
    best = select_best_photo([frame], detector, DetectionContext(camera_id="c"), {"person"}, crop=False)
    payload = best.as_dict()
    assert payload["detection"]["label"] == "person"
    assert 0.0 <= payload["score"] <= 1.0


async def test_mock_ai_provider_output_is_grounded_and_deterministic():
    provider = MockAIProvider(embedding_dimensions=16)
    context = AnalysisContext(
        camera_id="cam",
        camera_name="Driveway",
        event_type="person",
        detections=[PERSON],
        zone="driveway",
        tags=["person", "driveway-access", "driveway"],
    )
    first = await provider.analyze_image(b"frame", context)
    second = await provider.analyze_image(b"frame", context)
    assert first == second
    assert isinstance(first, ImageAnalysis)
    assert first.objects == ["person"]
    assert first.event_category == "person"
    assert 0.0 <= first.confidence <= 1.0
    assert first.actions


async def test_mock_ai_provider_does_not_invent_objects():
    provider = MockAIProvider(embedding_dimensions=8)
    context = AnalysisContext(camera_id="cam", camera_name="Yard", event_type="motion")
    analysis = await provider.analyze_image(b"frame", context)
    assert analysis.objects == []
    assert analysis.actions == []
    assert analysis.event_category == "unknown"


async def test_embeddings_have_configurable_width_and_unit_norm():
    provider = MockAIProvider(embedding_dimensions=32)
    embedding = await provider.create_embedding("person in driveway")
    assert len(embedding) == 32
    assert math.sqrt(sum(v * v for v in embedding)) == pytest.approx(1.0, abs=1e-3)
    assert embedding == await provider.create_embedding("person in driveway")
    assert embedding != await provider.create_embedding("dog in garden")


def test_assert_grounded_rejects_invented_objects():
    analysis = ImageAnalysis(
        summary="A bear appeared.",
        objects=["bear"],
        actions=[],
        event_category="animal",
        importance="high",
        confidence=0.9,
    )
    with pytest.raises(GroundingError):
        assert_grounded(analysis, {"dog"})


def _tone(frequency: float, *, amplitude: float, sample_rate: int = 16000, seconds: float = 0.25) -> bytes:
    count = int(sample_rate * seconds)
    return b"".join(
        struct.pack("<h", int(amplitude * 32767 * math.sin(2 * math.pi * frequency * i / sample_rate)))
        for i in range(count)
    )


def test_silence_is_not_speech_like():
    result = analyze_pcm(b"\x00\x00" * 4000)
    assert result.speech_like is False
    assert result.confidence == 0.0


def test_speech_band_tone_is_detected_as_speech_like():
    # ~440 Hz at 16 kHz gives a zero-crossing rate inside the speech band.
    result = analyze_pcm(_tone(440, amplitude=0.5))
    assert result.speech_like is True
    assert result.rms > DEFAULT_ENERGY_THRESHOLD
    assert 0.0 < result.confidence <= 0.95


def test_high_frequency_hiss_is_rejected():
    result = analyze_pcm(_tone(6000, amplitude=0.5))
    assert result.speech_like is False


def test_quiet_audio_is_rejected_even_in_the_speech_band():
    result = analyze_pcm(_tone(440, amplitude=0.001))
    assert result.speech_like is False


def test_short_buffer_is_handled():
    assert analyze_pcm(b"\x01").sample_count == 0
