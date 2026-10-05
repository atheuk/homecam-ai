"""Roles, disabled accounts and Google sign-in (additive).

Every account that exists before this migration had full access (there were
no roles), so the new ``role`` column is backfilled to ``admin`` for them and
only afterwards switched to the least-privileged ``pending`` default for rows
created later. Nothing is dropped or rewritten, so the previous API revision
keeps working against the migrated schema.
"""
from alembic import op
import sqlalchemy as sa

revision = "0017_google_auth"
down_revision = "0016_event_clips"


def upgrade():
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("role", sa.String(16), nullable=False, server_default="admin"))
        batch.add_column(sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("google_sub", sa.String(255), nullable=True))
        batch.add_column(sa.Column("google_email", sa.String(255), nullable=True))
    with op.batch_alter_table("users") as batch:
        batch.alter_column("role", existing_type=sa.String(16), server_default="pending")
    op.create_index("ix_users_google_sub", "users", ["google_sub"], unique=True)

    op.create_table(
        "oauth_login_states",
        sa.Column("state_hash", sa.String(64), primary_key=True),
        sa.Column("binding_hash", sa.String(64), nullable=False),
        sa.Column("nonce", sa.String(128), nullable=False),
        sa.Column("code_verifier", sa.String(128), nullable=False),
        sa.Column("intent", sa.String(16), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_oauth_login_states_expires_at", "oauth_login_states", ["expires_at"])


def downgrade():
    op.drop_index("ix_oauth_login_states_expires_at", table_name="oauth_login_states")
    op.drop_table("oauth_login_states")
    op.drop_index("ix_users_google_sub", table_name="users")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("google_email")
        batch.drop_column("google_sub")
        batch.drop_column("disabled_at")
        batch.drop_column("role")
