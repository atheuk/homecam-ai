"""Persistent vehicle tracks and mailbox/bin scene state"""
from alembic import op
import sqlalchemy as sa

revision = "0008_scene_state"
down_revision = "0007_person_trust"


def upgrade():
    op.create_table(
        "vehicle_tracks",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("camera_id", sa.String(64), nullable=False, index=True),
        sa.Column("label", sa.String(32), nullable=False),
        sa.Column("zone", sa.String(64), nullable=True),
        sa.Column("state", sa.String(16), nullable=False, index=True),
        sa.Column("x1", sa.Float, nullable=False),
        sa.Column("y1", sa.Float, nullable=False),
        sa.Column("x2", sa.Float, nullable=False),
        sa.Column("y2", sa.Float, nullable=False),
        sa.Column("observation_count", sa.Integer, nullable=False),
        sa.Column("first_seen_at", sa.Float, nullable=False),
        sa.Column("last_seen_at", sa.Float, nullable=False),
        sa.Column("anchored_at", sa.Float, nullable=False),
        sa.Column("stationary_since", sa.Float, nullable=True),
        sa.Column("departed_at", sa.Float, nullable=True),
        sa.Column("confidence", sa.Float, nullable=False),
        sa.Column("appearance", sa.JSON, nullable=False),
        sa.Column("data", sa.JSON, nullable=False),
        sa.Column("last_event_id", sa.String(64), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "scene_states",
        sa.Column("id", sa.String(160), primary_key=True),
        sa.Column("camera_id", sa.String(64), nullable=False, index=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("zone_id", sa.String(64), nullable=True),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("data", sa.JSON, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("scene_states")
    op.drop_table("vehicle_tracks")
