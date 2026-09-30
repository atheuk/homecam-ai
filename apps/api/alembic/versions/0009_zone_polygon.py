"""Optional polygon outline for camera zones

Existing rectangle zones keep ``points = NULL`` and behave exactly as
before; the bounding-box columns stay authoritative for region crops.
"""
from alembic import op
import sqlalchemy as sa

revision = "0009_zone_polygon"
down_revision = "0008_scene_state"


def upgrade():
    op.add_column("camera_zones", sa.Column("points", sa.JSON, nullable=True))


def downgrade():
    op.drop_column("camera_zones", "points")
