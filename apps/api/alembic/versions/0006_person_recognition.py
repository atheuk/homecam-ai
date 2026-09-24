"""Person recognition: durable event photos, identities and sightings"""
from alembic import op
import sqlalchemy as sa

revision = "0006_person_recognition"
down_revision = "0005_dahua_edge_mode"


def upgrade():
    op.add_column("events", sa.Column("person_id", sa.String(64), nullable=True))
    op.add_column("events", sa.Column("person_confidence", sa.Float(), nullable=True))
    op.add_column(
        "events",
        sa.Column("person_confirmed", sa.Boolean(), nullable=True, server_default=sa.false()),
    )
    op.add_column("events", sa.Column("photo_rating", sa.Integer(), nullable=True))
    op.add_column(
        "events", sa.Column("photo_rating_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index("ix_events_person_id", "events", ["person_id"])

    op.create_table(
        "event_photos",
        sa.Column("event_id", sa.String(64), sa.ForeignKey("events.id"), primary_key=True),
        sa.Column("image", sa.LargeBinary(), nullable=False),
        sa.Column("content_type", sa.String(64), nullable=False, server_default="image/jpeg"),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("caption", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "persons",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(120), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("centroid", sa.JSON(), nullable=True),
        sa.Column("embedding_dimensions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("samples", sa.JSON(), nullable=True),
        sa.Column("sighting_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cover_event_id", sa.String(64), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "person_sightings",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("person_id", sa.String(64), sa.ForeignKey("persons.id"), nullable=False),
        sa.Column("event_id", sa.String(64), sa.ForeignKey("events.id"), nullable=False),
        sa.Column("camera_id", sa.String(64), nullable=False),
        sa.Column("similarity", sa.Float(), nullable=True),
        sa.Column("assigned_by", sa.String(16), nullable=False, server_default="auto"),
        sa.Column("embedding", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_person_sightings_person_id", "person_sightings", ["person_id"])
    op.create_index("ix_person_sightings_event_id", "person_sightings", ["event_id"])
    op.create_index("ix_person_sightings_camera_id", "person_sightings", ["camera_id"])


def downgrade():
    op.drop_index("ix_person_sightings_camera_id", table_name="person_sightings")
    op.drop_index("ix_person_sightings_event_id", table_name="person_sightings")
    op.drop_index("ix_person_sightings_person_id", table_name="person_sightings")
    op.drop_table("person_sightings")
    op.drop_table("persons")
    op.drop_table("event_photos")
    op.drop_index("ix_events_person_id", table_name="events")
    op.drop_column("events", "photo_rating_at")
    op.drop_column("events", "photo_rating")
    op.drop_column("events", "person_confirmed")
    op.drop_column("events", "person_confidence")
    op.drop_column("events", "person_id")
