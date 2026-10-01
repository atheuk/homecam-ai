from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CameraOut(BaseModel):
    model_config=ConfigDict(from_attributes=True)
    id:str; provider_id:str; name:str; type:str; model:str; online:bool; status:str="online"
    battery_level:int|None=None; capabilities:dict[str,str]={}


class EventOut(BaseModel):
    model_config=ConfigDict(from_attributes=True)
    id:str; camera_id:str; type:str; priority:str; source:str; start_time:datetime; description:str
    event_metadata:dict={}


class MockEventIn(BaseModel):
    camera_id:str="mock-front-door"
    type:str="person"


class CameraStatusIn(BaseModel):
    status:str = Field(pattern="^(online|offline|degraded|unknown)$")


class CameraBatteryIn(BaseModel):
    battery_level:int = Field(ge=0, le=100)


class ProviderOutageIn(BaseModel):
    unavailable:bool = True


class PhotoRatingIn(BaseModel):
    """Human quality rating of an event's stored photo (1 = useless, 5 = perfect)."""

    rating: int | None = Field(default=None, ge=1, le=5)


class PersonAssignIn(BaseModel):
    """Assign (or correct) who is in an event.

    Exactly one of ``person_id`` (an existing identity) or ``name`` (create a
    new named identity from this event) is normally supplied. Supplying both
    assigns to the existing identity and renames it, which is what "this is
    actually Sarah, not Unknown person 3" means in practice.
    """

    person_id: str | None = None
    name: str | None = Field(default=None, max_length=120)


class PersonUpdateIn(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    notes: str | None = Field(default=None, max_length=2000)
    # A household decision, set by a person who knows them. HomeCam never
    # derives trust from appearance - doing so would be profiling, and it
    # cannot possibly be accurate about someone it has never met.
    trust: Literal["unknown", "trusted", "watch"] | None = None


class PersonMergeIn(BaseModel):
    """Fold one identity into another after a human confirms they match."""

    source_id: str


class MockAudioIn(BaseModel):
    """Raw mono 16-bit little-endian PCM, base64 encoded.

    HomeCam never synthesizes audio: the caller (a provider adapter, or a
    developer simulating one) supplies a real buffer.
    """

    pcm_base64: str = Field(min_length=1)
    sample_rate: int = Field(default=16000, ge=4000, le=48000)


class AudioAnalysisOut(BaseModel):
    camera_id: str
    speech_like: bool
    rms: float
    zero_crossing_rate: float
    confidence: float
    sample_count: int
    event_id: str | None = None


class RegisterIn(BaseModel):
    email:str
    password:str = Field(min_length=8, max_length=128)

    @field_validator("email")
    @classmethod
    def email_must_look_valid(cls, value: str) -> str:
        if "@" not in value or "." not in value.split("@")[-1]:
            raise ValueError("email must be a valid address")
        return value.lower().strip()


class LoginIn(BaseModel):
    email:str
    password:str


class UserOut(BaseModel):
    model_config=ConfigDict(from_attributes=True)
    id:str; email:str; created_at:datetime


class TokenOut(BaseModel):
    access_token:str
    token_type:str = "bearer"
    expires_at:datetime
    user:UserOut


class SecurityModeIn(BaseModel):
    mode: Literal["disarmed", "home", "away", "night"]


class SecurityModeOut(BaseModel):
    mode: str
    changed_by: str | None = None
    changed_at: datetime
    changed_source: str = "manual"
    # Schedule context: what the schedule wants right now, when it next
    # changes, and whether the current mode is a manual override of it.
    schedule: dict | None = None


class ArmingScheduleIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    mode: Literal["disarmed", "home", "away", "night"]
    days_of_week: list[int] = Field(min_length=1)
    start_time: str = Field(pattern=r"^\d{2}:\d{2}$")
    end_time: str = Field(pattern=r"^\d{2}:\d{2}$")
    enabled: bool = True
    priority: int = Field(default=0, ge=0, le=1000)


class ArmingScheduleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    mode: Literal["disarmed", "home", "away", "night"] | None = None
    days_of_week: list[int] | None = Field(default=None, min_length=1)
    start_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    end_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    enabled: bool | None = None
    priority: int | None = Field(default=None, ge=0, le=1000)


class ArmingScheduleOut(BaseModel):
    id: str
    name: str
    mode: str
    days_of_week: list[int]
    start_time: str
    end_time: str
    enabled: bool
    priority: int


class IntegrationModeIn(BaseModel):
    """Mode change requested by an external automation (Home Assistant)."""

    mode: Literal["disarmed", "home", "away", "night"]
    # Free-text label recorded in the audit trail so "who armed the house"
    # stays answerable, e.g. "home-assistant:everyone-left".
    source: str = Field(default="integration", min_length=1, max_length=64)


class RetentionHoldIn(BaseModel):
    hold: bool


class RetentionReportOut(BaseModel):
    dry_run: bool
    started_at: datetime
    cutoffs: dict
    counts: dict
    protected: dict
    truncated: bool


class RetentionPolicyOut(BaseModel):
    policy: dict
    report: RetentionReportOut


class RetentionPurgeIn(BaseModel):
    # Defaults to a dry run: deleting data must be the explicit choice.
    dry_run: bool = True


class IncidentOut(BaseModel):
    id: str
    kind: str
    status: str
    severity: str
    camera_id: str
    zone: str | None = None
    mode_at_creation: str
    event_ids: list[str]
    event_count: int
    first_seen_at: datetime
    last_seen_at: datetime
    acknowledged_by: str | None = None
    acknowledged_at: datetime | None = None
    resolved_by: str | None = None
    resolved_at: datetime | None = None
    escalation_level: int
    last_escalated_at: datetime | None = None
    summary: str
    ai_summary: str | None = None
    evidence: dict | None = None
    clip: dict
    clip_hold: bool = False
    created_at: datetime
    updated_at: datetime


class IncidentExportOut(BaseModel):
    incident: IncidentOut
    events: list[EventOut]
    exported_at: datetime


class AuditLogOut(BaseModel):
    id: str
    actor_user_id: str | None = None
    actor_label: str | None = None
    action: str
    target_type: str | None = None
    target_id: str | None = None
    details: dict
    created_at: datetime



class DeterrenceActionIn(BaseModel):
    """A *proposal* to run a deterrent. Creating one never executes it."""

    camera_id: str
    # Closed set - deliberately contains nothing that contacts a third
    # party or emergency services. See app/services/deterrence.py.
    action: Literal["siren", "light", "voice"]
    reason: str = Field(default="", max_length=300)
    incident_id: str | None = None


class DeterrenceActionOut(BaseModel):
    id: str
    camera_id: str
    action: str
    status: str
    reason: str
    incident_id: str | None = None
    requested_by: str | None = None
    confirmed_by: str | None = None
    result: str | None = None
    created_at: datetime
    expires_at: datetime
    resolved_at: datetime | None = None


class DeterrenceCapabilityOut(BaseModel):
    action: str
    supported: bool
    detail: str


class DeterrenceCapabilitiesOut(BaseModel):
    enabled: bool
    provider: str
    # Always True. Present in the payload so any client can see, without
    # reading the docs, that HomeCam will not fire a deterrent on its own.
    requires_human_confirmation: bool
    actions: list[DeterrenceCapabilityOut]