"""Create the transactional outbox."""
from alembic import op
from app.persistence.migrations import create_group, drop_group
revision = "0007_outbox"
down_revision = "0006_actions"
branch_labels = None
depends_on = None

def upgrade() -> None:
    create_group(op.get_bind(), "outbox")

def downgrade() -> None:
    drop_group(op.get_bind(), "outbox")
