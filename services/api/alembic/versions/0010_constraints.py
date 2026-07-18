"""Install deferred cross-domain constraints and lookup indexes."""
from alembic import op
from app.persistence.migrations import create_indexes, drop_indexes
revision = "0010_constraints"
down_revision = "0009_operations"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_foreign_key(
        "fk_assignments_brief_source_id_sources", "assignments", "sources",
        ["brief_source_id"], ["id"], ondelete="SET NULL",
    )
    create_indexes(op.get_bind(), owner=False)

def downgrade() -> None:
    drop_indexes(op.get_bind(), owner=False)
    op.drop_constraint("fk_assignments_brief_source_id_sources", "assignments", type_="foreignkey")
