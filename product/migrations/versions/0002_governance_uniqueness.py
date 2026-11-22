"""Enforce cleanup identity and template version uniqueness in the database."""
from alembic import op

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade():
    # Fail rather than silently discard rows if existing data contains duplicates.
    op.create_index('uq_cleanup_resource', 'cleanup_items', ['resource_type', 'resource_id'], unique=True)
    op.create_index('uq_template_version', 'templates', ['name', 'version'], unique=True)


def downgrade():
    raise RuntimeError('Destructive schema rollback requires an approved recovery procedure.')
