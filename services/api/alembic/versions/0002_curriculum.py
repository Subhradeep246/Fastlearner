"""Create curriculum and reviewed-content tables."""
from alembic import op
from app.persistence.migrations import create_group, drop_group
revision = "0002_curriculum"
down_revision = "0001_identity"
branch_labels = None
depends_on = None

def upgrade() -> None:
    create_group(op.get_bind(), "curriculum")

def downgrade() -> None:
    drop_group(op.get_bind(), "curriculum")
