"""Validated AI output schemas (SPEC section 15).

Model output is never trusted as prose: it must parse into
:class:`ImageAnalysis`, whose object/action lists are additionally checked
against the detections that actually happened, so an AI provider cannot
introduce activity that no camera observed.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

EventCategory = Literal[
    "delivery", "visitor", "vehicle", "animal", "person", "motion", "mailbox", "unknown"
]
Importance = Literal["low", "normal", "high", "critical"]


class ImageAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=500)
    objects: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    event_category: EventCategory = "unknown"
    importance: Importance = "normal"
    confidence: float = Field(ge=0.0, le=1.0)


class AIAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str
    event_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)


class GroundingError(ValueError):
    """Raised when AI output mentions objects no detector actually saw."""


def assert_grounded(analysis: ImageAnalysis, observed_labels: set[str]) -> ImageAnalysis:
    """Reject analyses that name objects outside the observed detections.

    SPEC section 15: "Do not let the LLM invent activity." Grounding is
    enforced in HomeCam Core, not left to prompt discipline.
    """
    invented = {obj for obj in analysis.objects if obj not in observed_labels}
    if invented:
        raise GroundingError(f"AI output referenced unobserved objects: {sorted(invented)}")
    return analysis
