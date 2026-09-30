"""Modern AI security features: zone dwell, loitering presence, digests, deterrence

Adds two nullable columns (``camera_zones.dwell_seconds`` and
``incidents.evidence``) and three new tables (``zone_presence``,
``daily_digests``, ``deterrence_actions``). Nothing existing is modified or
dropped, and the new columns are nullable so existing zones keep falling
back to the configured default dwell window and existing incidents keep
their current shape.
"""
from alembic import op
import sqlalchemy as sa

revision = "0011_modern_ai_security"
down_revision = "0010_security_essentials"


def upgrade():
    op.add_column("camera_zones", sa.Column("dwell_seconds", sa.Float, nullable=True))
    op.add_column("incidents", sa.Column("evidence", sa.JSON, nullable=True))

    op.create_table(
        "zone_presence",
        sa.Column("id", sa.String(200), primary_key=True),
        sa.Column("camera_id", sa.String(64), nullable=False),
        sa.Column("zone", sa.String(64), nullable=False),
        sa.Column("label", sa.String(32), nullable=False, server_default="person"),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_alert_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_zone_presence_camera_id", "zone_presence", ["camera_id"])

    op.create_table(
        "daily_digests",
        sa.Column("date", sa.String(10), primary_key=True),
        sa.Column("summary", sa.Text, nullable=False, server_default=""),
        sa.Column("source", sa.String(32), nullable=False, server_default="template"),
        sa.Column("stats", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "deterrence_actions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("camera_id", sa.String(64), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("reason", sa.String(300), nullable=False, server_default=""),
        sa.Column("incident_id", sa.String(64), nullable=True),
        sa.Column("requested_by", sa.String(64), nullable=True),
        sa.Column("confirmed_by", sa.String(64), nullable=True),
        sa.Column("result", sa.String(300), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_deterrence_actions_camera_id", "deterrence_actions", ["camera_id"])
    op.create_index("ix_deterrence_actions_status", "deterrence_actions", ["status"])


def downgrade():
    op.drop_index("ix_deterrence_actions_status", table_name="deterrence_actions")
    op.drop_index("ix_deterrence_actions_camera_id", table_name="deterrence_actions")
    op.drop_table("deterrence_actions")

    op.drop_table("daily_digests")

    op.drop_index("ix_zone_presence_camera_id", table_name="zone_presence")
    op.drop_table("zone_presence")

    op.drop_column("incidents", "evidence")
    op.drop_column("camera_zones", "dwell_seconds")
