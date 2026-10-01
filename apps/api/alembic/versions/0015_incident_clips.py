"""Store bounded incident clips in shared storage."""
from alembic import op
import sqlalchemy as sa

revision = "0015_incident_clips"
down_revision = "0014_notifications"


def upgrade():
    op.add_column("camera_zones", sa.Column("alerts_enabled", sa.Boolean, nullable=False, server_default=sa.true()))
    op.add_column("incidents", sa.Column("clip_status", sa.String(16), nullable=True))
    op.add_column("incidents", sa.Column("clip_hold", sa.Boolean, nullable=False, server_default=sa.false()))
    op.create_table(
        "incident_clips",
        sa.Column("incident_id", sa.String(64), sa.ForeignKey("incidents.id"), primary_key=True),
        sa.Column("video", sa.LargeBinary, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("incident_clips")
    op.drop_column("incidents", "clip_hold")
    op.drop_column("incidents", "clip_status")
    op.drop_column("camera_zones", "alerts_enabled")
