"""Security essentials: arming modes, incidents, audit trail, auth lockout

Adds three new tables (``security_states``, ``incidents``, ``audit_logs``)
and two new nullable/defaulted columns on ``users`` (``failed_attempts``,
``locked_until``). All new columns have safe defaults so existing rows and
existing tests are unaffected; no existing column/table is modified.
"""
from alembic import op
import sqlalchemy as sa

revision = "0010_security_essentials"
down_revision = "0009_zone_polygon"


def upgrade():
    op.add_column(
        "users",
        sa.Column("failed_attempts", sa.Integer, nullable=False, server_default="0"),
    )
    op.add_column("users", sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "security_states",
        sa.Column("id", sa.String(16), primary_key=True),
        sa.Column("mode", sa.String(16), nullable=False, server_default="disarmed"),
        sa.Column("changed_by", sa.String(64), nullable=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "incidents",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("severity", sa.String(16), nullable=False, server_default="low"),
        sa.Column("camera_id", sa.String(64), nullable=False),
        sa.Column("zone", sa.String(64), nullable=True),
        sa.Column("mode_at_creation", sa.String(16), nullable=False, server_default="disarmed"),
        sa.Column("event_ids", sa.JSON, nullable=False),
        sa.Column("event_count", sa.Integer, nullable=False, server_default="1"),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acknowledged_by", sa.String(64), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.String(64), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("escalation_level", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_escalated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("summary", sa.String(500), nullable=False, server_default=""),
        sa.Column("ai_summary", sa.String(1000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_incidents_kind", "incidents", ["kind"])
    op.create_index("ix_incidents_status", "incidents", ["status"])
    op.create_index("ix_incidents_camera_id", "incidents", ["camera_id"])

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("actor_user_id", sa.String(64), nullable=True),
        sa.Column("actor_label", sa.String(255), nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target_type", sa.String(32), nullable=True),
        sa.Column("target_id", sa.String(64), nullable=True),
        sa.Column("details", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_audit_logs_actor_user_id", "audit_logs", ["actor_user_id"])
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"])
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])


def downgrade():
    op.drop_index("ix_audit_logs_created_at", table_name="audit_logs")
    op.drop_index("ix_audit_logs_action", table_name="audit_logs")
    op.drop_index("ix_audit_logs_actor_user_id", table_name="audit_logs")
    op.drop_table("audit_logs")

    op.drop_index("ix_incidents_camera_id", table_name="incidents")
    op.drop_index("ix_incidents_status", table_name="incidents")
    op.drop_index("ix_incidents_kind", table_name="incidents")
    op.drop_table("incidents")

    op.drop_table("security_states")

    op.drop_column("users", "locked_until")
    op.drop_column("users", "failed_attempts")
