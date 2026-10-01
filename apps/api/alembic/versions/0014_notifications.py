"""Instant incident alerts

Adds the outbound notification plane: configured channels (Web Push, ntfy,
Telegram, generic webhook), per-user Web Push subscriptions, the
delivery/dedupe ledger that keeps two API replicas from double-sending the
same incident, and the household-wide notification policy row.
"""
from alembic import op
import sqlalchemy as sa

revision = "0014_notifications"
down_revision = "0013_arming_and_retention"


def upgrade():
    op.create_table(
        "notification_channels",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("type", sa.String(16), nullable=False, index=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("config", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("secret_encrypted", sa.String(2048), nullable=True),
        sa.Column("attach_images", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("min_severity", sa.String(16), nullable=False, server_default="low"),
        sa.Column("last_status", sa.String(16), nullable=True),
        sa.Column("last_message", sa.String(500), nullable=True),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "push_subscriptions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("endpoint", sa.String(500), nullable=False, unique=True),
        sa.Column("p256dh", sa.String(255), nullable=False),
        sa.Column("auth", sa.String(255), nullable=False),
        sa.Column("user_agent", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_count", sa.Integer, nullable=False, server_default="0"),
    )
    op.create_table(
        "notification_deliveries",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("channel_id", sa.String(64), nullable=False, index=True),
        sa.Column("dedupe_key", sa.String(160), nullable=False),
        sa.Column("incident_id", sa.String(64), nullable=True, index=True),
        sa.Column("reason", sa.String(32), nullable=False, server_default="created"),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending", index=True),
        sa.Column("detail", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.UniqueConstraint("channel_id", "dedupe_key", name="uq_notification_delivery"),
    )
    op.create_table(
        "notification_settings",
        sa.Column("id", sa.String(16), primary_key=True),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("quiet_hours_enabled", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("quiet_hours_start", sa.String(5), nullable=False, server_default="22:00"),
        sa.Column("quiet_hours_end", sa.String(5), nullable=False, server_default="07:00"),
        sa.Column("quiet_hours_override_severity", sa.String(16), nullable=False, server_default="critical"),
        sa.Column("min_severity", sa.String(16), nullable=False, server_default="low"),
        sa.Column("max_per_hour", sa.Integer, nullable=False, server_default="20"),
        sa.Column("updated_by", sa.String(64), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("notification_settings")
    op.drop_table("notification_deliveries")
    op.drop_table("push_subscriptions")
    op.drop_table("notification_channels")
