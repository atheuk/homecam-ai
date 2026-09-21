"""Shared capability keys for the SPEC section 5 capability status map.

``audioDetection`` is new in the AI pipeline phase. It advertises whether a
camera can feed an audio buffer into the speech-like-activity stage in
``app/ai/audio.py`` — not whether the camera has a speaker (that remains
``twoWayAudio``).

Default is ``UNAVAILABLE`` for every provider: no provider currently exposes
a normalized audio buffer API, and HomeCam must never fake audio data. A
provider that gains one only has to return ``SUPPORTED`` here.
"""
from __future__ import annotations

from .base import CapabilityStatus

AUDIO_DETECTION = "audioDetection"


def audio_detection_status(provider_exposes_audio: bool, enabled: bool) -> str:
    """Resolve the ``audioDetection`` status for one camera.

    - provider has no audio API  -> UNAVAILABLE (nothing to analyze)
    - provider has audio, feature off -> UNSUPPORTED (explicitly disabled)
    - provider has audio, feature on  -> SUPPORTED
    """
    if not provider_exposes_audio:
        return CapabilityStatus.UNAVAILABLE.value
    return CapabilityStatus.SUPPORTED.value if enabled else CapabilityStatus.UNSUPPORTED.value
