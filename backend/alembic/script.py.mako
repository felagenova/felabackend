% if autogenerate %
"""
Revision ID: ${up_revision}
Revises: ${down_revision | commajoin}
Create Date: ${create_date}
"""
% else %
"""
Revision ID: ${up_revision}
Create Date: ${create_date}
"""
% endif

from alembic import op
import sqlalchemy as sa

revision = '${up_revision}'
down_revision = ${repr(down_revision)}
branch_labels = None
depends_on = None

def upgrade():
    pass


def downgrade():
    pass
