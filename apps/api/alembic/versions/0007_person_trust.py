"""Person trust status, set by a human rather than inferred"""
from alembic import op
import sqlalchemy as sa

revision = "0007_person_trust"
down_revision = "0006_person_recognition"


def upgrade():
    # Existing identities start as "unknown": nobody has vouched for them
    # yet, and defaulting anyone to trusted would silently suppress the
    # alerts this column exists to control.
    op.add_column(
        "persons",
        sa.Column("trust", sa.String(16), nullable=False, server_default="unknown"),
    )


def downgrade():
    op.drop_column("persons", "trust")
