"""Create deliberate-memory and vector tables."""
from alembic import op
from app.persistence.migrations import create_group, drop_group
revision = "0005_memory_vector"
down_revision = "0004_learning"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    create_group(op.get_bind(), "memory")

def downgrade() -> None:
    drop_group(op.get_bind(), "memory")
