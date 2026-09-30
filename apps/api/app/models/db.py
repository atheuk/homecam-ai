from datetime import datetime
from sqlalchemy import String, DateTime, Boolean, Float, JSON, Integer, ForeignKey, LargeBinary, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

class Base(DeclarativeBase): pass
class Camera(Base):
    __tablename__ = "cameras"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider_id: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(120))
    type: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(120))
    online: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(16), default="online")
    battery_level: Mapped[int|None] = mapped_column(Integer, nullable=True)
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict)
class Event(Base):
    __tablename__ = "events"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    camera_id: Mapped[str] = mapped_column(String(64), index=True)
    type: Mapped[str] = mapped_column(String(32))
    priority: Mapped[str] = mapped_column(String(16), default="normal")
    source: Mapped[str] = mapped_column(String(32), default="provider")
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    description: Mapped[str] = mapped_column(String(500))
    event_metadata: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    # Spec-compatible extension (SPEC section 9/31): the ``type`` enum is left
    # untouched; richer semantics ("car parked on the driveway", "mailbox
    # opened") are carried by the nullable zone plus the tags list.
    zone: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    thumbnail_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    best_photo_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    ai_analysis_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    activity_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # Identity of the person seen in this event, once recognized or assigned
    # by a human. NULL for non-person events and for person events whose crop
    # could not be embedded.
    person_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # Cosine similarity that produced ``person_id`` when it was assigned
    # automatically; NULL when a human assigned/corrected it.
    person_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    # True once a human explicitly confirmed or corrected ``person_id``.
    # Human-confirmed sightings are the only ones allowed to *teach* the
    # matcher new reference vectors, so one bad auto-match cannot poison an
    # identity into matching everyone.
    person_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    # Human 1-5 rating of how good/usable the stored photo is.
    photo_rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    photo_rating_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EventPhoto(Base):
    """The actual best-photo bytes for an event.

    Deliberately stored in the database rather than on ``media_root``: the
    deployed API runs on Azure Container Apps, whose per-replica filesystem
    is ephemeral, so a path written by one replica is a 404 from another and
    vanishes entirely on the next revision/restart. The photo is the whole
    point of the person-recognition feature, so it must outlive the replica
    that captured it. Rows are small (a cropped JPEG) and bounded by the
    event cooldown, so this stays well within Postgres' comfort zone.
    """

    __tablename__ = "event_photos"
    event_id: Mapped[str] = mapped_column(String(64), ForeignKey("events.id"), primary_key=True)
    image: Mapped[bytes] = mapped_column(LargeBinary)
    content_type: Mapped[str] = mapped_column(String(64), default="image/jpeg")
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Plain-language description of what is visible, so the photo is
    # understandable without squinting at a crop.
    caption: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Person(Base):
    """A recurring individual seen across events.

    A person is created automatically the first time an unrecognized face/body
    crop is embedded, and starts out unnamed ("Unknown person 3") until a human
    names it. ``centroid`` is the running mean of every reference vector, and
    is what new sightings are compared against.
    """

    __tablename__ = "persons"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # NULL until a human names them; the API renders a stable placeholder.
    name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Whether the household considers this person expected. Set by a human,
    # never inferred from how they look: the system has no business deciding
    # who looks trustworthy. "unknown" until someone says otherwise.
    trust: Mapped[str] = mapped_column(String(16), default="unknown")
    centroid: Mapped[list] = mapped_column(JSON, default=list)
    embedding_dimensions: Mapped[int] = mapped_column(Integer, default=0)
    # Bounded list of reference vectors (most recent wins) used to recompute
    # the centroid when a sighting is added or a bad match is corrected away.
    samples: Mapped[list] = mapped_column(JSON, default=list)
    sighting_count: Mapped[int] = mapped_column(Integer, default=0)
    # Event id whose photo best represents this person (highest human rating,
    # else most recent), used as their avatar.
    cover_event_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PersonSighting(Base):
    """One observation of a person, linking an event to an identity.

    Kept separate from ``Event.person_id`` (which is the denormalized "who is
    in this event" answer) so the full history of matches/corrections is
    auditable: what was matched, how confidently, and whether a human agreed.
    """

    __tablename__ = "person_sightings"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    person_id: Mapped[str] = mapped_column(String(64), ForeignKey("persons.id"), index=True)
    event_id: Mapped[str] = mapped_column(String(64), ForeignKey("events.id"), index=True)
    camera_id: Mapped[str] = mapped_column(String(64), index=True)
    similarity: Mapped[float | None] = mapped_column(Float, nullable=True)
    # "auto" (matched by embedding), "new" (first sighting of a new identity)
    # or "manual" (a human assigned/corrected it).
    assigned_by: Mapped[str] = mapped_column(String(16), default="auto")
    embedding: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CameraZone(Base):
    """User-defined named region in normalized image coordinates.

    Zones carry no vision logic of their own: they are labels plus a shape.
    ``x1/y1/x2/y2`` is always the zone's bounding box; ``points`` optionally
    holds the exact drawn polygon (``[[x, y], ...]``, >= 3 normalized
    points). Overlap matching uses the polygon when present, while the
    region-based mailbox/bin detectors keep cropping the bounding box.
    """

    __tablename__ = "camera_zones"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    camera_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(32), default="other")
    x1: Mapped[float] = mapped_column(Float)
    y1: Mapped[float] = mapped_column(Float)
    x2: Mapped[float] = mapped_column(Float)
    y2: Mapped[float] = mapped_column(Float)
    points: Mapped[list | None] = mapped_column(JSON, nullable=True, default=None)
    # Seconds a person may stay continuously inside this zone before it
    # counts as loitering. NULL falls back to
    # ``settings.zone_default_dwell_seconds``; zones a household considers
    # "fine to stand in" can be given a long threshold instead of being
    # excluded entirely.
    dwell_seconds: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ZonePresence(Base):
    """How long a subject has been continuously present in one zone.

    Deliberately a *database* row rather than in-process state (unlike
    ``app.ai.dwell``'s frame-level tracker): the API can run as two
    replicas, and consecutive sightings of the same loiterer may well be
    handled by different replicas. The primary key is the
    camera+zone+label tuple, so the row is the single point of
    serialization for that tuple across every replica.
    """

    __tablename__ = "zone_presence"
    id: Mapped[str] = mapped_column(String(200), primary_key=True)
    camera_id: Mapped[str] = mapped_column(String(64), index=True)
    zone: Mapped[str] = mapped_column(String(64))
    label: Mapped[str] = mapped_column(String(32), default="person")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_alert_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DailyDigest(Base):
    """One generated day-in-review summary, keyed by its local date.

    The date is the primary key so "generate yesterday's digest" is
    idempotent across replicas at the database level: the second writer's
    INSERT fails and it re-reads the winner's row instead of producing a
    duplicate.
    """

    __tablename__ = "daily_digests"
    date: Mapped[str] = mapped_column(String(10), primary_key=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(32), default="template")
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DeterrenceAction(Base):
    """A requested siren/light/voice action and its human confirmation.

    A row is created in ``pending`` and is only ever executed after an
    authenticated human explicitly confirms it (see
    ``app.services.deterrence``). Nothing in HomeCam may transition this
    to ``executed`` on its own.
    """

    __tablename__ = "deterrence_actions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    camera_id: Mapped[str] = mapped_column(String(64), index=True)
    action: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    reason: Mapped[str] = mapped_column(String(300), default="")
    incident_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    requested_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confirmed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AIAnalysis(Base):
    """AI analysis persistence (SPEC section 32).

    ``embedding`` is stored as a JSON array of floats with its dimensionality
    recorded alongside it, so it stays portable across SQLite (host-native
    tests) and the pgvector-enabled PostgreSQL used by Compose. See
    docs/ai-pipeline.md for the documented pgvector follow-up.
    """

    __tablename__ = "ai_analyses"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(64), ForeignKey("events.id"), index=True)
    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(120))
    summary: Mapped[str] = mapped_column(String(500))
    objects: Mapped[list] = mapped_column(JSON, default=list)
    actions: Mapped[list] = mapped_column(JSON, default=list)
    category: Mapped[str] = mapped_column(String(32), default="unknown")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    embedding: Mapped[list] = mapped_column(JSON, default=list)
    embedding_dimensions: Mapped[int] = mapped_column(Integer, default=0)
    detections: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Activity(Base):
    """Cross-camera correlated activity (SPEC sections 18/19)."""

    __tablename__ = "activities"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    category: Mapped[str] = mapped_column(String(32), default="unknown")
    summary: Mapped[str] = mapped_column(String(500))
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    event_ids: Mapped[list] = mapped_column(JSON, default=list)
    cameras: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class VehicleTrack(Base):
    """A vehicle followed across frames, independently of event cooldowns.

    Persisted so a restart does not re-announce a car that has been parked
    in view all along. Times are epoch seconds (the tracker's clock).
    """

    __tablename__ = "vehicle_tracks"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    camera_id: Mapped[str] = mapped_column(String(64), index=True)
    label: Mapped[str] = mapped_column(String(32))
    zone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # tentative -> tracking -> stable, and departed once gone.
    state: Mapped[str] = mapped_column(String(16), default="tentative", index=True)
    # Box the vehicle is being held to ("same place" is IoU with this).
    x1: Mapped[float] = mapped_column(Float)
    y1: Mapped[float] = mapped_column(Float)
    x2: Mapped[float] = mapped_column(Float)
    y2: Mapped[float] = mapped_column(Float)
    observation_count: Mapped[int] = mapped_column(Integer, default=1)
    first_seen_at: Mapped[float] = mapped_column(Float)
    last_seen_at: Mapped[float] = mapped_column(Float)
    anchored_at: Mapped[float] = mapped_column(Float)
    stationary_since: Mapped[float | None] = mapped_column(Float, nullable=True)
    departed_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    appearance: Mapped[list] = mapped_column(JSON, default=list)
    # Transient counters/flags (reported, interaction streak, ...).
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    last_event_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SceneState(Base):
    """Persisted state of one watched thing on a camera.

    ``kind`` is ``camera`` (frame continuity), ``mailbox`` or ``bin``; the
    latter two are keyed by their zone. ``data`` holds the state machine.
    """

    __tablename__ = "scene_states"
    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    camera_id: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    zone_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    state: Mapped[str] = mapped_column(String(32), default="unknown")
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Auth hardening (SPEC section 27 follow-up, OWASP IoT/ASVS brute-force
    # guidance): consecutive bad passwords since the last success, and the
    # timestamp until which login is refused regardless of password
    # correctness. Both reset to 0/NULL on a successful login.
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
class AuthSession(Base):
    __tablename__ = "auth_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
class ProviderConfig(Base):
    """Runtime (DB-backed) provider connection settings (SPEC admin plane).

    A single table covers both supported provider types; fields unused by a
    given ``provider_type`` stay ``NULL``. Secrets (Dahua password / Eufy
    adapter token) are only ever stored encrypted in ``secret_encrypted`` and
    are never included in API responses; see ``app/crypto.py``.
    """
    __tablename__ = "provider_configs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider_type: Mapped[str] = mapped_column(String(16), index=True)
    name: Mapped[str] = mapped_column(String(120))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # Dahua only: "direct" (raw LAN HTTP/RTSP, default) or "edge" (Home
    # Assistant/Raspberry Pi edge connector over a private VPN/overlay).
    # Unused (NULL) for Eufy, which is always adapter/edge-shaped.
    mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    scheme: Mapped[str | None] = mapped_column(String(8), nullable=True)
    host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    username: Mapped[str | None] = mapped_column(String(120), nullable=True)
    channels: Mapped[str | None] = mapped_column(String(500), nullable=True)
    adapter_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    secret_encrypted: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    last_test_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    last_test_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SecurityState(Base):
    """Household arming state (SPEC follow-up: arming/home/away/night modes).

    A single row (``id="default"``) since HomeCam is a single-household,
    single-system deployment (see ``ProviderConfig``/``User`` for the same
    assumption elsewhere). ``mode`` is one of ``disarmed``, ``home``,
    ``away``, ``night`` and is purely a *human decision*, never inferred:
    changing it always requires an authenticated user and is audit-logged.

    This state never gates whether events are detected/stored/searchable —
    only whether a detected event also raises an actionable ``Incident``
    (see ``app/services/security_modes.py`` and ``app/services/incidents.py``).
    Camera-health incidents (tamper/obstruction/offline) are not gated by
    this at all: system integrity is not the same thing as intrusion
    detection, matching how a physical alarm panel still reports a cut wire
    while disarmed.
    """

    __tablename__ = "security_states"
    id: Mapped[str] = mapped_column(String(16), primary_key=True, default="default")
    mode: Mapped[str] = mapped_column(String(16), default="disarmed")
    changed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Incident(Base):
    """An actionable grouping of one or more events (SPEC follow-up:
    incident grouping/timeline, acknowledgment/escalation, evidence export).

    Deliberately separate from ``Event``: an event is "something the
    detector saw"; an incident is "something a human may need to act on".
    New alert-worthy events for the same camera+zone+kind merge into an
    existing open incident within ``incident_merge_window_seconds`` instead
    of raising a duplicate (SPEC follow-up: detection confidence/dedup),
    bumping ``event_count``/``last_seen_at``/``severity`` rather than
    creating a new row.
    """

    __tablename__ = "incidents"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # "intrusion" (a gated subject event) or "camera_health" (tamper /
    # obstruction / offline). Camera-health incidents are never suppressed
    # by arming mode.
    kind: Mapped[str] = mapped_column(String(24), index=True)
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    severity: Mapped[str] = mapped_column(String(16), default="low")
    camera_id: Mapped[str] = mapped_column(String(64), index=True)
    zone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # The arming mode in effect when this incident was opened, so the
    # timeline/export always explains *why* an event became actionable.
    mode_at_creation: Mapped[str] = mapped_column(String(16), default="disarmed")
    event_ids: Mapped[list] = mapped_column(JSON, default=list)
    event_count: Mapped[int] = mapped_column(Integer, default=1)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    acknowledged_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Bumped by the escalation sweep for unacknowledged incidents past a
    # time threshold; purely a severity/urgency signal surfaced to the UI
    # and audit trail. HomeCam never pages/dispatches anyone automatically.
    escalation_level: Mapped[int] = mapped_column(Integer, default=0)
    last_escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Deterministic, template-built description - always present.
    summary: Mapped[str] = mapped_column(String(500), default="")
    # Optional AI-assisted risk summary (Foundry chat deployment), same
    # opt-in/degrade-gracefully pattern as photo captioning: absent unless
    # Foundry is configured, and never authoritative on its own.
    ai_summary: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    # Structured, incident-kind-specific evidence pointers (currently the
    # before/after package snapshot pair captured by the scene tracker).
    # Always a reference to evidence the pipeline already stored - this is
    # never a second copy of any imagery.
    evidence: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    """Append-only record of security-relevant actions (SPEC follow-up:
    audit trail). Covers auth events (login/lockout/logout/session revoke),
    arming-mode changes, and incident acknowledge/resolve/escalate. Never
    covers camera imagery/content - only who did what, to what, and when.
    """

    __tablename__ = "audit_logs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # Human-readable actor label for actions with no authenticated user yet
    # (e.g. a failed login attempt for an unknown/locked email).
    actor_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    target_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

