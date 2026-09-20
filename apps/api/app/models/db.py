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

