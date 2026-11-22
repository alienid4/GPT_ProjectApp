"""Represent cleared capacity as unknown rather than an invented zero."""
from alembic import op
import sqlalchemy as sa

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('capacities') as batch:
        batch.alter_column('hours', existing_type=sa.Numeric(7, 2), nullable=True)


def downgrade():
    raise RuntimeError('Destructive downgrade is disabled; restore a verified matching backup instead.')
