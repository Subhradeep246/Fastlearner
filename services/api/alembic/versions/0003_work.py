"""Create assignments, goals, and planning tables."""
from alembic import op
from app.persistence.migrations import create_group, drop_group
revision = "0003_work"
down_revision = "0002_curriculum"
branch_labels = None
depends_on = None

def upgrade() -> None:
    create_group(op.get_bind(), "work")

def downgrade() -> None:
    drop_group(op.get_bind(), "work")
