"""Automatic arming schedules and enforced data retention

Adds the ``arming_schedules`` table plus the two columns the scheduler and
the purge job serialize on: ``security_states.last_transition_at`` (the
scheduled boundary already applied, which is also what makes a manual
override expire at the next boundary) and ``events.retention_hold`` (an
explicit human "keep this" that outranks the retention policy).
"""
from alembic import op
import sqlalchemy as sa

revision = "0013_arming_schedules_and_retention"
down_revision = "0012_ingestion_leases"


def upgrade():
    op.create_table(
        "arming_schedules",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("days_of_week", sa.JSON, nullable=False),
        sa.Column("start_time", sa.String(5), nullable=False),
        sa.Column("end_time", sa.String(5), nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("priority", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.add_column(
        "security_states",
        sa.Column("changed_source", sa.String(16), nullable=False, server_default="manual"),
    )
    op.add_column(
        "security_states",
        sa.Column("last_transition_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "events",
        sa.Column("retention_hold", sa.Boolean, nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_events_retention_hold", "events", ["retention_hold"])


def downgrade():
    op.drop_index("ix_events_retention_hold", table_name="events")
    op.drop_column("events", "retention_hold")
    op.drop_column("security_states", "last_transition_at")
    op.drop_column("security_states", "changed_source")
    op.drop_table("arming_schedules")
