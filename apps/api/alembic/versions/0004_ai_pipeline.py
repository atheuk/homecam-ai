"""AI pipeline: event zones/tags, camera zones, ai analyses, activities"""
from alembic import op
import sqlalchemy as sa

revision = "0004_ai_pipeline"
down_revision = "0003_provider_configs"


def upgrade():
    # Spec-compatible extension of the event model (SPEC 9/31): the type
    # enum is unchanged; richer semantics ride on zone + tags.
    op.add_column("events", sa.Column("zone", sa.String(64), nullable=True))
    op.add_column("events", sa.Column("tags", sa.JSON(), nullable=True))
    op.add_column("events", sa.Column("thumbnail_path", sa.String(500), nullable=True))
    op.add_column("events", sa.Column("best_photo_path", sa.String(500), nullable=True))
    op.add_column("events", sa.Column("ai_analysis_id", sa.String(64), nullable=True))
    op.add_column("events", sa.Column("activity_id", sa.String(64), nullable=True))
    op.create_index("ix_events_zone", "events", ["zone"])
    op.create_index("ix_events_activity_id", "events", ["activity_id"])

    op.create_table(
        "camera_zones",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("camera_id", sa.String(64), index=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False, server_default="other"),
        sa.Column("x1", sa.Float(), nullable=False),
        sa.Column("y1", sa.Float(), nullable=False),
        sa.Column("x2", sa.Float(), nullable=False),
        sa.Column("y2", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )

    # SPEC 32. ``embedding`` is a JSON float array with its dimensionality
    # stored alongside it so the same schema works on SQLite (host-native
    # tests) and on the pgvector-enabled PostgreSQL used by Compose; see
    # docs/ai-pipeline.md for the native ``vector`` column follow-up.
    op.create_table(
        "ai_analyses",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("event_id", sa.String(64), sa.ForeignKey("events.id"), index=True),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("model", sa.String(120), nullable=False),
        sa.Column("summary", sa.String(500), nullable=False),
        sa.Column("objects", sa.JSON()),
        sa.Column("actions", sa.JSON()),
        sa.Column("category", sa.String(32), nullable=False, server_default="unknown"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("embedding", sa.JSON()),
        sa.Column("embedding_dimensions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("detections", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True)),
    )

    op.create_table(
        "activities",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("start_time", sa.DateTime(timezone=True), index=True),
        sa.Column("end_time", sa.DateTime(timezone=True)),
        sa.Column("category", sa.String(32), nullable=False, server_default="unknown"),
        sa.Column("summary", sa.String(500), nullable=False, server_default=""),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("event_ids", sa.JSON()),
        sa.Column("cameras", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )


def downgrade():
    op.drop_table("activities")
    op.drop_table("ai_analyses")
    op.drop_table("camera_zones")
    op.drop_index("ix_events_activity_id", table_name="events")
    op.drop_index("ix_events_zone", table_name="events")
    for column in ("activity_id", "ai_analysis_id", "best_photo_path", "thumbnail_path", "tags", "zone"):
        op.drop_column("events", column)
