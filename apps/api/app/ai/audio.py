"""Voice / speech-like audio activity detection (capability ``audioDetection``).

Scope is deliberately narrow and honest: this is a classic energy +
zero-crossing-rate voice-activity heuristic over raw 16-bit PCM. It detects
*speech-like audio activity*. It is **not** speech recognition, transcription
or speaker identification, and it must never be presented as such.

Because most providers do not currently expose an audio buffer, the
``audioDetection`` capability is ``UNAVAILABLE`` by default; see
``app/providers/capabilities.py``. The analysis stage below is real and
tested, so a provider that later gains an audio API only has to hand raw PCM
to :func:`analyze_pcm`.
"""
from __future__ import annotations

import array
import math
from dataclasses import dataclass

# Human speech sits roughly in this zero-crossing band for 8-16 kHz PCM:
# below it is usually hum/rumble, far above it is hiss/noise.
SPEECH_ZCR_MIN = 0.02
SPEECH_ZCR_MAX = 0.28
DEFAULT_ENERGY_THRESHOLD = 0.02


@dataclass(frozen=True)
class AudioAnalysis:
    speech_like: bool
    rms: float
    zero_crossing_rate: float
    confidence: float
    sample_count: int

    def as_dict(self) -> dict:
        return {
            "speech_like": self.speech_like,
            "rms": round(self.rms, 5),
            "zero_crossing_rate": round(self.zero_crossing_rate, 5),
            "confidence": round(self.confidence, 4),
            "sample_count": self.sample_count,
        }


def _samples(pcm: bytes) -> array.array:
    usable = len(pcm) - (len(pcm) % 2)
    values = array.array("h")
    values.frombytes(pcm[:usable])
    return values


def analyze_pcm(pcm: bytes, energy_threshold: float = DEFAULT_ENERGY_THRESHOLD) -> AudioAnalysis:
    """Analyze mono 16-bit little-endian PCM for speech-like activity."""
    samples = _samples(pcm)
    if len(samples) < 2:
        return AudioAnalysis(False, 0.0, 0.0, 0.0, len(samples))

    scaled = [value / 32768.0 for value in samples]
    rms = math.sqrt(sum(value * value for value in scaled) / len(scaled))
    crossings = sum(
        1 for i in range(1, len(scaled)) if (scaled[i - 1] >= 0) != (scaled[i] >= 0)
    )
    zcr = crossings / (len(scaled) - 1)

    loud_enough = rms >= energy_threshold
    in_speech_band = SPEECH_ZCR_MIN <= zcr <= SPEECH_ZCR_MAX
    speech_like = loud_enough and in_speech_band

    # Confidence blends "clearly audible" with "how centred in the speech
    # ZCR band", and stays uncertainty-aware (capped below 1.0) per SPEC 15.
    energy_confidence = min(rms / max(energy_threshold * 4, 1e-6), 1.0)
    band_centre = (SPEECH_ZCR_MIN + SPEECH_ZCR_MAX) / 2
    band_width = (SPEECH_ZCR_MAX - SPEECH_ZCR_MIN) / 2
    band_confidence = max(0.0, 1.0 - abs(zcr - band_centre) / band_width)
    confidence = round(min(0.95, 0.5 * energy_confidence + 0.5 * band_confidence), 4) if speech_like else 0.0

    return AudioAnalysis(
        speech_like=speech_like,
        rms=rms,
        zero_crossing_rate=zcr,
        confidence=confidence,
        sample_count=len(samples),
    )
