"""add inbox_item to object_type check constraints

Revision ID: 0021
Revises: 0020
Create Date: 2026-05-08
"""
from alembic import op

revision = "0021"
down_revision = "0020_merge_graph_node_attrs_and_l7"
branch_labels = None
depends_on = None

OLD_TYPES = (
    "'asset', 'document', 'block', 'chunk', 'conversation', 'message', "
    "'raw_event', 'memory_candidate', 'memory', 'context_pack', 'job', "
    "'pipeline_def', 'pipeline_run', 'project', 'provider_model', 'credential', "
    "'review_item', 'import_run', 'backup', 'restore', 'external'"
)
NEW_TYPES = OLD_TYPES + ", 'inbox_item'"

CONSTRAINTS = [
    ("object_registry", "object_registry_object_type_check"),
    ("object_versions", "object_versions_object_type_check"),
]


def upgrade() -> None:
    for table, constraint in CONSTRAINTS:
        op.drop_constraint(constraint, table, type_="check")
        op.create_check_constraint(
            constraint,
            table,
            f"object_type IN ({NEW_TYPES})",
        )


def downgrade() -> None:
    for table, constraint in CONSTRAINTS:
        op.drop_constraint(constraint, table, type_="check")
        op.create_check_constraint(
            constraint,
            table,
            f"object_type IN ({OLD_TYPES})",
        )
