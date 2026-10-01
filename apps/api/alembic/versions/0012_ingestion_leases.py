"""Per-camera ingestion leader lease

Adds the ``ingestion_leases`` table. With several API replicas only the
lease holder ingests a camera and advances its scene state, so replicas no
longer overwrite each other's vehicle, bin and mailbox state. ``epoch`` is the fencing token checked by every scene write.
"""
from alembic import op
import sqlalchemy as sa

revision = "0012_ingestion_leases"
down_revision = "0011_modern_ai_security"


def upgrade():
    op.create_table(
        "ingestion_leases",
        sa.Column("camera_id", sa.String(64), primary_key=True),
        sa.Column("holder", sa.String(128), nullable=False),
        sa.Column("epoch", sa.Integer, nullable=False, server_default="1"),
        sa.Column("expires_at", sa.Float, nullable=False),
        sa.Column("acquired_at", sa.Float, nullable=False),
    )


def downgrade():
    op.drop_table("ingestion_leases")
