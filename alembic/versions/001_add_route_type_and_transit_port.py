"""add route_type and transit_port

Revision ID: 001
Revises: 
Create Date: 2026-04-26 14:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('ocean_rates', sa.Column('route_type', sa.String(length=20), nullable=True, comment="直达/中转 (Direct/Transit)"))
    op.add_column('ocean_rates', sa.Column('transit_port', sa.String(length=50), nullable=True, comment="中转港"))


def downgrade():
    op.drop_column('ocean_rates', 'transit_port')
    op.drop_column('ocean_rates', 'route_type')
