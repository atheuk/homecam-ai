"""Store bounded short clips for ordinary events."""
from alembic import op
import sqlalchemy as sa

revision = "0016_event_clips"
down_revision = "0015_incident_clips"


def upgrade():
    op.create_table(
        "event_clips",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("camera_id", sa.String(128), nullable=False),
        sa.Column("video", sa.LargeBinary, nullable=False),
        sa.Column("content_type", sa.String(64), nullable=False, server_default="video/mp4"),
        sa.Column("size_bytes", sa.Integer, nullable=False),
        sa.Column("duration_ms", sa.Integer, nullable=False, server_default="0"),
        sa.Column("pre_roll_ms", sa.Integer, nullable=False, server_default="0"),
        sa.Column("source", sa.String(16), nullable=False, server_default="stream"),
        sa.Column("source_ref", sa.String(160), nullable=True, unique=True),
        sa.Column("event_ids", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_event_clips_camera_id", "event_clips", ["camera_id"])
    op.create_index("ix_event_clips_created_at", "event_clips", ["created_at"])


def downgrade():
    op.drop_index("ix_event_clips_created_at", table_name="event_clips")
    op.drop_index("ix_event_clips_camera_id", table_name="event_clips")
    op.drop_table("event_clips")
