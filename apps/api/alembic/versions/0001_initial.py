"""initial camera and event tables"""
from alembic import op
import sqlalchemy as sa
revision="0001_initial"; down_revision=None
def upgrade():
 op.create_table("cameras",sa.Column("id",sa.String(64),primary_key=True),sa.Column("provider_id",sa.String(64)),sa.Column("name",sa.String(120)),sa.Column("type",sa.String(32)),sa.Column("model",sa.String(120)),sa.Column("online",sa.Boolean()),sa.Column("battery_level",sa.Integer()),sa.Column("capabilities",sa.JSON()))
 op.create_table("events",sa.Column("id",sa.String(64),primary_key=True),sa.Column("camera_id",sa.String(64)),sa.Column("type",sa.String(32)),sa.Column("priority",sa.String(16)),sa.Column("source",sa.String(32)),sa.Column("start_time",sa.DateTime(timezone=True)),sa.Column("description",sa.String(500)),sa.Column("metadata",sa.JSON()))
def downgrade(): op.drop_table("events");op.drop_table("cameras")
