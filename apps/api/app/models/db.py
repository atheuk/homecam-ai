from datetime import datetime
from sqlalchemy import String, DateTime, Boolean, JSON, Integer, ForeignKey
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

