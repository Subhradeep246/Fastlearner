"""Install owner-first indexes on every owner-scoped table."""
from alembic import op
from app.persistence.migrations import create_indexes, drop_indexes
revision = "0011_owner_indexes"
down_revision = "0010_constraints"
branch_labels = None
depends_on = None

def upgrade() -> None:
    create_indexes(op.get_bind(), owner=True)

def downgrade() -> None:
    drop_indexes(op.get_bind(), owner=True)
