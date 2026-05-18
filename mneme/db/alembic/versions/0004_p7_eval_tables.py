"""P7 Eval DDL: create eval_tasks + eval_tables tables.

Revision ID: 0004_p7_eval_tables
Revises: 0003_p7_graph_tables
Create Date: 2026-05-05

eval_tasks
----------
Evaluation tasks for benchmarking memory extraction, retrieval, and other
pipeline components.  Each task has a ``task_type`` (precision_recall, BLEU,
ROUGE, F1, accuracy, manual, custom) and a ``status`` state machine::

    pending → running → completed
    pending → running → failed
    pending → cancelled
    running → cancelled

eval_results
------------
Per-item evaluation results linked to an eval task.  Each result stores an
``input_text``, ``expected_output``, ``actual_output``, and a ``metrics_json``
payload with per-metric scores (e.g. {"precision": 0.85, "recall": 0.72}).

Design rationale
----------------
* Separate from pipeline tables — eval is a cross-cutting concern that can
  benchmark any pipeline stage.
* ``metrics_json`` uses jsonb for schema-flexible metric storage.
* Results are append-only; once written, they are not modified.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004_p7_eval_tables"
down_revision: str | Sequence[str] | None = "0003_p7_graph_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ═══════════════════════════════════════════════════════════════════════
# DDL SQL fragments
# ═══════════════════════════════════════════════════════════════════════

_EVAL_TASKS_SQL = r"""
CREATE TABLE eval_tasks (
    task_id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    task_name         varchar(200) NOT NULL,
    task_type         varchar(40) NOT NULL DEFAULT 'precision_recall',
    description       text,
    status            varchar(24) NOT NULL DEFAULT 'pending',
    progress          numeric(5,2) NOT NULL DEFAULT 0.0,
    config_json       jsonb NOT NULL DEFAULT '{}'::jsonb,
    total_items       integer NOT NULL DEFAULT 0,
    processed_items   integer NOT NULL DEFAULT 0,
    created_by_user_id uuid,
    started_at        timestamptz,
    finished_at       timestamptz,
    error_message     text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),

    CHECK (task_type IN (
        'precision_recall', 'bleu', 'rouge', 'f1',
        'accuracy', 'manual', 'custom'
    )),
    CHECK (status IN (
        'pending', 'running', 'completed', 'failed', 'cancelled'
    )),
    CHECK (progress >= 0 AND progress <= 100)
);
"""

_EVAL_RESULTS_SQL = r"""
CREATE TABLE eval_results (
    result_id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id           uuid NOT NULL,
    item_index        integer NOT NULL DEFAULT 0,
    input_text        text,
    expected_output   text,
    actual_output     text,
    metrics_json      jsonb NOT NULL DEFAULT '{}'::jsonb,
    metadata_json     jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at        timestamptz NOT NULL DEFAULT now()
);
"""

_FK_SQL = r"""
ALTER TABLE eval_results
    ADD CONSTRAINT fk_eval_results_task
    FOREIGN KEY (task_id) REFERENCES eval_tasks(task_id)
    ON DELETE CASCADE;

ALTER TABLE eval_tasks
    ADD CONSTRAINT fk_eval_tasks_user
    FOREIGN KEY (created_by_user_id) REFERENCES users(user_id)
    ON DELETE SET NULL;
"""

_INDEX_SQL = r"""
CREATE INDEX idx_eval_tasks_status_type
    ON eval_tasks(status, task_type, created_at DESC);

CREATE INDEX idx_eval_results_task
    ON eval_results(task_id, item_index);
"""

_TRIGGER_SQL = r"""
CREATE TRIGGER trg_eval_tasks_updated_at
    BEFORE UPDATE ON eval_tasks
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
"""

# ═══════════════════════════════════════════════════════════════════════
# Downgrade SQL
# ═══════════════════════════════════════════════════════════════════════

_DOWNGRADE_SQL = r"""
DROP TRIGGER IF EXISTS trg_eval_tasks_updated_at ON eval_tasks;
DROP TABLE IF EXISTS eval_results CASCADE;
DROP TABLE IF EXISTS eval_tasks CASCADE;
"""


def upgrade() -> None:
    op.execute(_EVAL_TASKS_SQL)
    op.execute(_EVAL_RESULTS_SQL)
    op.execute(_FK_SQL)
    op.execute(_INDEX_SQL)
    op.execute(_TRIGGER_SQL)


def downgrade() -> None:
    op.execute(_DOWNGRADE_SQL)
