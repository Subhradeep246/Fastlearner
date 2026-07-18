"""Create graph, deletion, and export lifecycle tables."""
from alembic import op
from app.persistence.migrations import create_group, drop_group
revision = "0008_lifecycle"
down_revision = "0007_outbox"
branch_labels = None
depends_on = None

def upgrade() -> None:
    create_group(op.get_bind(), "lifecycle")

def downgrade() -> None:
    drop_group(op.get_bind(), "lifecycle")
