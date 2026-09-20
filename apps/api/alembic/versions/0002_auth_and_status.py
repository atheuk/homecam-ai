"""camera status column, users, and auth sessions"""
from alembic import op
import sqlalchemy as sa

revision = "0002_auth_and_status"
down_revision = "0001_initial"


def upgrade():
    op.add_column("cameras", sa.Column("status", sa.String(16), nullable=False, server_default="online"))
    op.create_table(
        "users",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("email", sa.String(255), unique=True, index=True),
        sa.Column("password_hash", sa.String(255)),
        sa.Column("created_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "auth_sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), sa.ForeignKey("users.id"), index=True),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
    )


def downgrade():
    op.drop_table("auth_sessions")
    op.drop_table("users")
    op.drop_column("cameras", "status")
