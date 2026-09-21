"""runtime provider configuration (admin plane)"""
from alembic import op
import sqlalchemy as sa

revision = "0003_provider_configs"
down_revision = "0002_auth_and_status"


def upgrade():
    op.create_table(
        "provider_configs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("provider_type", sa.String(16), index=True),
        sa.Column("name", sa.String(120)),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("scheme", sa.String(8), nullable=True),
        sa.Column("host", sa.String(255), nullable=True),
        sa.Column("port", sa.Integer(), nullable=True),
        sa.Column("username", sa.String(120), nullable=True),
        sa.Column("channels", sa.String(500), nullable=True),
        sa.Column("adapter_url", sa.String(255), nullable=True),
        sa.Column("secret_encrypted", sa.String(2048), nullable=True),
        sa.Column("last_test_status", sa.String(16), nullable=True),
        sa.Column("last_test_message", sa.String(500), nullable=True),
        sa.Column("last_test_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )


def downgrade():
    op.drop_table("provider_configs")
