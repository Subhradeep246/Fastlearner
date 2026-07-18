"""Create identity and authorization tables."""
from alembic import op
from app.persistence.migrations import create_group, drop_group
revision = "0001_identity"
down_revision = None
branch_labels = None
depends_on = None

def upgrade() -> None:
    create_group(op.get_bind(), "identity")

def downgrade() -> None:
    drop_group(op.get_bind(), "identity")
