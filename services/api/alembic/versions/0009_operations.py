"""Create worker, rule, and diagnostic operation tables."""
from alembic import op
from app.persistence.migrations import create_group, drop_group
revision = "0009_operations"
down_revision = "0008_lifecycle"
branch_labels = None
depends_on = None

def upgrade() -> None:
    create_group(op.get_bind(), "operations")

def downgrade() -> None:
    drop_group(op.get_bind(), "operations")
