"""Create action, idempotency, and audit tables."""
from alembic import op
from app.persistence.migrations import create_group, drop_group
revision = "0006_actions"
down_revision = "0005_memory_vector"
branch_labels = None
depends_on = None

def upgrade() -> None:
    create_group(op.get_bind(), "actions")

def downgrade() -> None:
    drop_group(op.get_bind(), "actions")
