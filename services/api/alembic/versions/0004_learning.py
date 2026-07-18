"""Create learning and review-state tables."""
from alembic import op
from app.persistence.migrations import create_group, drop_group
revision = "0004_learning"
down_revision = "0003_work"
branch_labels = None
depends_on = None

def upgrade() -> None:
    create_group(op.get_bind(), "learning")

def downgrade() -> None:
    drop_group(op.get_bind(), "learning")
