"""Dahua edge connector mode (Home Assistant/Raspberry Pi bridge)"""
from alembic import op
import sqlalchemy as sa

revision = "0005_dahua_edge_mode"
down_revision = "0004_ai_pipeline"


def upgrade():
    op.add_column(
        "provider_configs",
        sa.Column("mode", sa.String(16), nullable=True, server_default="direct"),
    )


def downgrade():
    op.drop_column("provider_configs", "mode")
