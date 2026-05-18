"""P7 Eval A/B Test DDL: create eval_ab_tests + eval_ab_results tables.

Revision ID: 0017_eval_ab_tables
Revises: 0016_neg_space_and_trust
Create Date: 2026-05-07

eval_ab_tests
-------------
A/B comparison test definitions. Each test compares two parameter variants
(variant A vs variant B) on a shared benchmark task_type. Status state machine::

    pending → running → completed
    pending → running → failed
    pending → cancelled
    running → cancelled

eval_ab_results
---------------
Per-metric A/B comparison deltas. Each row stores the statistical comparison
for one metric: mean_a, mean_b, delta, Cohen's d, p-value, significance,
and per-item win/tie/loss counts.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0017_eval_ab_tables"
down_revision: str | Sequence[str] | None = "0016_neg_space_and_trust"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ═══════════════════════════════════════════════════════════════════════
# DDL SQL fragments
# ═══════════════════════════════════════════════════════════════════════

_EVAL_AB_TESTS_SQL = r"""
CREATE TABLE eval_ab_tests (
    ab_test_id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    test_name             varchar(200) NOT NULL,
    description           text,
    variant_a_label       varchar(100) NOT NULL,
    variant_b_label       varchar(100) NOT NULL,
    task_type             varchar(80) NOT NULL,
    config_a_json         jsonb NOT NULL DEFAULT '{}'::jsonb,
    config_b_json         jsonb NOT NULL DEFAULT '{}'::jsonb,
    status                varchar(24) NOT NULL DEFAULT 'pending',
    recommendation        text,
    significant_metrics   integer NOT NULL DEFAULT 0,
    total_metrics         integer NOT NULL DEFAULT 0,
    project_id            uuid,
    created_by_user_id    uuid,
    started_at            timestamptz,
    finished_at           timestamptz,
    error_message         text,
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),

    CHECK (status IN (
        'pending', 'running', 'completed', 'failed', 'cancelled'
    ))
);
"""

_EVAL_AB_RESULTS_SQL = r"""
CREATE TABLE eval_ab_results (
    result_id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ab_test_id        uuid NOT NULL,
    metric_name       varchar(100) NOT NULL,
    mean_a            double precision NOT NULL DEFAULT 0.0,
    mean_b            double precision NOT NULL DEFAULT 0.0,
    delta             double precision NOT NULL DEFAULT 0.0,
    delta_pct         double precision NOT NULL DEFAULT 0.0,
    std_a             double precision NOT NULL DEFAULT 0.0,
    std_b             double precision NOT NULL DEFAULT 0.0,
    cohens_d          double precision NOT NULL DEFAULT 0.0,
    effect_size       varchar(20) NOT NULL DEFAULT '',
    p_value           double precision,
    significant       boolean NOT NULL DEFAULT false,
    winner            varchar(10) NOT NULL DEFAULT '',
    wins_a            integer NOT NULL DEFAULT 0,
    wins_b            integer NOT NULL DEFAULT 0,
    ties              integer NOT NULL DEFAULT 0,
    sample_count_a    integer NOT NULL DEFAULT 0,
    sample_count_b    integer NOT NULL DEFAULT 0,
    created_at        timestamptz NOT NULL DEFAULT now()
);
"""

_FK_SQL = r"""
ALTER TABLE eval_ab_results
    ADD CONSTRAINT fk_eval_ab_results_test
    FOREIGN KEY (ab_test_id) REFERENCES eval_ab_tests(ab_test_id)
    ON DELETE CASCADE;

ALTER TABLE eval_ab_tests
    ADD CONSTRAINT fk_eval_ab_tests_user
    FOREIGN KEY (created_by_user_id) REFERENCES users(user_id)
    ON DELETE SET NULL;

ALTER TABLE eval_ab_tests
    ADD CONSTRAINT fk_eval_ab_tests_project
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
    ON DELETE SET NULL;
"""

_INDEX_SQL = r"""
CREATE INDEX idx_eval_ab_tests_status
    ON eval_ab_tests(status, created_at DESC);

CREATE INDEX idx_eval_ab_tests_task_type
    ON eval_ab_tests(task_type, created_at DESC);

CREATE INDEX idx_eval_ab_results_test
    ON eval_ab_results(ab_test_id, metric_name);
"""

_TRIGGER_SQL = r"""
CREATE TRIGGER trg_eval_ab_tests_updated_at
    BEFORE UPDATE ON eval_ab_tests
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
"""

# ═══════════════════════════════════════════════════════════════════════
# Downgrade SQL
# ═══════════════════════════════════════════════════════════════════════

_DOWNGRADE_SQL = r"""
DROP TRIGGER IF EXISTS trg_eval_ab_tests_updated_at ON eval_ab_tests;
DROP TABLE IF EXISTS eval_ab_results CASCADE;
DROP TABLE IF EXISTS eval_ab_tests CASCADE;
"""


def upgrade() -> None:
    op.execute(_EVAL_AB_TESTS_SQL)
    op.execute(_EVAL_AB_RESULTS_SQL)
    op.execute(_FK_SQL)
    op.execute(_INDEX_SQL)
    op.execute(_TRIGGER_SQL)


def downgrade() -> None:
    op.execute(_DOWNGRADE_SQL)
