from datetime import datetime
from sqlalchemy import String, DateTime, Boolean, Float, JSON, Integer, ForeignKey
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


class CameraZone(Base):
    """User-defined named rectangle in normalized image coordinates.

    Zones carry no vision logic of their own: they are labels plus a box,
    and the pipeline only computes bbox overlap against them.
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


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


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
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

