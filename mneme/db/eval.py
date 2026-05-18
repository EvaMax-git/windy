"""P7 Eval data-access layer.

Provides CRUD against ``eval_tasks``, ``eval_results``, ``eval_ab_tests``,
and ``eval_ab_results`` tables.
All writes use raw SQL for consistency with the project's existing patterns.
"""

from __future__ import annotations

import json
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from mneme.schemas.eval import ABTestCreate, EvalTaskCreate


# ═══════════════════════════════════════════════════════════════════════════
# Eval Tasks
# ═══════════════════════════════════════════════════════════════════════════

_INSERT_TASK = text(r"""
    INSERT INTO eval_tasks (
        task_id, task_name, task_type, description,
        status, progress, config_json,
        total_items, processed_items,
        created_by_user_id,
        created_at, updated_at
    ) VALUES (
        :task_id, :task_name, :task_type, :description,
        'pending', 0, :config_json,
        0, 0,
        :created_by_user_id,
        :now, :now
    )
""")


def create_eval_task(
    db: Session,
    *,
    payload: EvalTaskCreate,
    created_by_user_id: UUID | None = None,
) -> dict:
    """Create a new eval task and return the row as a dict."""
    from datetime import datetime, timezone
    task_id = uuid4()
    now = datetime.now(timezone.utc)
    db.execute(
        _INSERT_TASK,
        {
            "task_id": task_id,
            "task_name": payload.task_name,
            "task_type": payload.task_type,
            "description": payload.description,
            "config_json": json.dumps(payload.config),
            "created_by_user_id": created_by_user_id,
            "now": now,
        },
    )
    db.commit()
    # Read back the row (avoids RETURNING which is PostgreSQL-specific)
    row = db.execute(_GET_TASK, {"task_id": task_id}).mappings().first()
    return dict(row) if row else {}


_LIST_TASKS = text(r"""
    SELECT
        task_id, task_name, task_type, description,
        status, progress, config_json,
        total_items, processed_items,
        created_by_user_id,
        started_at, finished_at, error_message,
        created_at, updated_at
    FROM eval_tasks
    WHERE (:status IS NULL OR status = :status)
      AND (:task_type IS NULL OR task_type = :task_type)
    ORDER BY created_at DESC
    LIMIT :limit OFFSET :offset
""")

_COUNT_TASKS = text(r"""
    SELECT COUNT(*) FROM eval_tasks
    WHERE (:status IS NULL OR status = :status)
      AND (:task_type IS NULL OR task_type = :task_type)
""")


def list_eval_tasks(
    db: Session,
    *,
    status: str | None = None,
    task_type: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[dict], int]:
    """Return (items, total) for eval tasks."""
    offset = (max(page, 1) - 1) * page_size
    total = db.execute(
        _COUNT_TASKS,
        {"status": status, "task_type": task_type},
    ).scalar_one()

    rows = db.execute(
        _LIST_TASKS,
        {
            "status": status,
            "task_type": task_type,
            "limit": page_size,
            "offset": offset,
        },
    ).mappings().all()

    items = [dict(r) for r in rows]
    return items, total or 0


_GET_TASK = text(r"""
    SELECT
        task_id, task_name, task_type, description,
        status, progress, config_json,
        total_items, processed_items,
        created_by_user_id,
        started_at, finished_at, error_message,
        created_at, updated_at
    FROM eval_tasks
    WHERE task_id = :task_id
""")


def get_eval_task(db: Session, *, task_id: UUID) -> dict | None:
    """Return a single eval task row or None."""
    row = db.execute(_GET_TASK, {"task_id": task_id}).mappings().first()
    return dict(row) if row else None


_UPDATE_TASK_STATUS = text(r"""
    UPDATE eval_tasks
    SET status = :status,
        started_at = CASE WHEN :status = 'running' THEN :now ELSE started_at END,
        finished_at = CASE WHEN :status IN ('completed', 'failed', 'cancelled') THEN :now ELSE finished_at END,
        error_message = :error_message,
        updated_at = :now
    WHERE task_id = :task_id
""")


def update_eval_task_status(
    db: Session,
    *,
    task_id: UUID,
    status: str,
    error_message: str | None = None,
) -> dict | None:
    """Update an eval task's status. Returns updated row or None."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    db.execute(
        _UPDATE_TASK_STATUS,
        {"task_id": task_id, "status": status, "error_message": error_message, "now": now},
    )
    db.commit()
    row = db.execute(_GET_TASK, {"task_id": task_id}).mappings().first()
    return dict(row) if row else None


# ═══════════════════════════════════════════════════════════════════════════
# Eval Results
# ═══════════════════════════════════════════════════════════════════════════

_LIST_RESULTS = text(r"""
    SELECT
        result_id, task_id, item_index,
        input_text, expected_output, actual_output,
        metrics_json, metadata_json,
        created_at
    FROM eval_results
    WHERE task_id = :task_id
    ORDER BY item_index ASC
    LIMIT :limit OFFSET :offset
""")

_COUNT_RESULTS = text(r"""
    SELECT COUNT(*) FROM eval_results WHERE task_id = :task_id
""")


def list_eval_results(
    db: Session,
    *,
    task_id: UUID,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[dict], int]:
    """Return (items, total) for eval results of a task."""
    offset = (max(page, 1) - 1) * page_size
    total = db.execute(_COUNT_RESULTS, {"task_id": task_id}).scalar_one()

    rows = db.execute(
        _LIST_RESULTS,
        {"task_id": task_id, "limit": page_size, "offset": offset},
    ).mappings().all()

    items = [dict(r) for r in rows]
    return items, total or 0


# ── Metric aggregation for task detail ──────────────────────────────────

_AGGREGATE_METRICS = text(r"""
    SELECT
        metric_name,
        AVG(metric_value)    AS agg_value,
        MIN(metric_value)    AS min_value,
        MAX(metric_value)    AS max_value,
        STDDEV(metric_value) AS std_dev,
        COUNT(*)             AS sample_count
    FROM (
        SELECT
            (jsonb_each_text(metrics_json)).key   AS metric_name,
            (jsonb_each_text(metrics_json)).value::numeric AS metric_value
        FROM eval_results
        WHERE task_id = :task_id
    ) sub
    GROUP BY metric_name
    ORDER BY metric_name
""")


def get_eval_metrics_summary(
    db: Session,
    *,
    task_id: UUID,
) -> list[dict]:
    """Return aggregated metric summaries for a task."""
    rows = db.execute(_AGGREGATE_METRICS, {"task_id": task_id}).mappings().all()
    results = []
    for r in rows:
        results.append({
            "metric_name": r["metric_name"],
            "aggregation": "mean",
            "value": float(r["agg_value"] or 0),
            "min_value": float(r["min_value"] or 0),
            "max_value": float(r["max_value"] or 0),
            "std_dev": float(r["std_dev"] or 0),
            "sample_count": int(r["sample_count"] or 0),
        })
    return results


# ═══════════════════════════════════════════════════════════════════════════
# A/B Tests
# ═══════════════════════════════════════════════════════════════════════════

_INSERT_AB_TEST = text(r"""
    INSERT INTO eval_ab_tests (
        ab_test_id, test_name, description,
        variant_a_label, variant_b_label, task_type,
        config_a_json, config_b_json, status,
        project_id, created_by_user_id,
        created_at, updated_at
    ) VALUES (
        :ab_test_id, :test_name, :description,
        :variant_a_label, :variant_b_label, :task_type,
        CAST(:config_a_json AS jsonb), CAST(:config_b_json AS jsonb),
        'pending',
        :project_id, :created_by_user_id,
        NOW(), NOW()
    )
    RETURNING ab_test_id, test_name, description,
              variant_a_label, variant_b_label, task_type,
              config_a_json, config_b_json, status,
              recommendation, significant_metrics, total_metrics,
              project_id, created_by_user_id,
              started_at, finished_at, error_message,
              created_at, updated_at
""")


def create_ab_test(
    db: Session,
    *,
    payload: ABTestCreate,
    created_by_user_id: UUID | None = None,
) -> dict:
    """Create a new A/B test and return the row as a dict."""
    ab_test_id = uuid4()
    row = db.execute(
        _INSERT_AB_TEST,
        {
            "ab_test_id": ab_test_id,
            "test_name": payload.test_name,
            "description": payload.description,
            "variant_a_label": payload.variant_a.label,
            "variant_b_label": payload.variant_b.label,
            "task_type": payload.variant_a.task_type,
            "config_a_json": json.dumps(payload.variant_a.params),
            "config_b_json": json.dumps(payload.variant_b.params),
            "project_id": payload.project_id,
            "created_by_user_id": created_by_user_id,
        },
    ).mappings().one()
    db.commit()
    return dict(row)


_LIST_AB_TESTS = text(r"""
    SELECT
        ab_test_id, test_name, description,
        variant_a_label, variant_b_label, task_type,
        config_a_json, config_b_json, status,
        recommendation, significant_metrics, total_metrics,
        project_id, created_by_user_id,
        started_at, finished_at, error_message,
        created_at, updated_at
    FROM eval_ab_tests
    WHERE (:status IS NULL OR status = :status)
      AND (:task_type IS NULL OR task_type = :task_type)
    ORDER BY created_at DESC
    LIMIT :limit OFFSET :offset
""")

_COUNT_AB_TESTS = text(r"""
    SELECT COUNT(*) FROM eval_ab_tests
    WHERE (:status IS NULL OR status = :status)
      AND (:task_type IS NULL OR task_type = :task_type)
""")


def list_ab_tests(
    db: Session,
    *,
    status: str | None = None,
    task_type: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[dict], int]:
    """Return (items, total) for A/B tests."""
    offset = (max(page, 1) - 1) * page_size
    total = db.execute(
        _COUNT_AB_TESTS,
        {"status": status, "task_type": task_type},
    ).scalar_one()

    rows = db.execute(
        _LIST_AB_TESTS,
        {
            "status": status,
            "task_type": task_type,
            "limit": page_size,
            "offset": offset,
        },
    ).mappings().all()

    items = [dict(r) for r in rows]
    return items, total or 0


_GET_AB_TEST = text(r"""
    SELECT
        ab_test_id, test_name, description,
        variant_a_label, variant_b_label, task_type,
        config_a_json, config_b_json, status,
        recommendation, significant_metrics, total_metrics,
        project_id, created_by_user_id,
        started_at, finished_at, error_message,
        created_at, updated_at
    FROM eval_ab_tests
    WHERE ab_test_id = :ab_test_id
""")


def get_ab_test(db: Session, *, ab_test_id: UUID) -> dict | None:
    """Return a single A/B test row or None."""
    row = db.execute(_GET_AB_TEST, {"ab_test_id": ab_test_id}).mappings().first()
    return dict(row) if row else None


_UPDATE_AB_TEST_STATUS = text(r"""
    UPDATE eval_ab_tests
    SET status = :status,
        started_at = CASE WHEN :status = 'running' THEN NOW() ELSE started_at END,
        finished_at = CASE WHEN :status IN ('completed', 'failed', 'cancelled') THEN NOW() ELSE finished_at END,
        error_message = :error_message,
        updated_at = NOW()
    WHERE ab_test_id = :ab_test_id
    RETURNING ab_test_id, test_name, description,
              variant_a_label, variant_b_label, task_type,
              config_a_json, config_b_json, status,
              recommendation, significant_metrics, total_metrics,
              project_id, created_by_user_id,
              started_at, finished_at, error_message,
              created_at, updated_at
""")


def update_ab_test_status(
    db: Session,
    *,
    ab_test_id: UUID,
    status: str,
    error_message: str | None = None,
) -> dict | None:
    """Update an A/B test's status. Returns updated row or None."""
    row = db.execute(
        _UPDATE_AB_TEST_STATUS,
        {"ab_test_id": ab_test_id, "status": status, "error_message": error_message},
    ).mappings().first()
    db.commit()
    return dict(row) if row else None


_SAVE_AB_COMPARISON = text(r"""
    UPDATE eval_ab_tests
    SET recommendation = :recommendation,
        significant_metrics = :significant_metrics,
        total_metrics = :total_metrics,
        status = 'completed',
        finished_at = NOW(),
        updated_at = NOW()
    WHERE ab_test_id = :ab_test_id
""")


def save_ab_comparison(
    db: Session,
    *,
    ab_test_id: UUID,
    recommendation: str,
    significant_metrics: int,
    total_metrics: int,
) -> None:
    """Persist A/B comparison summary results."""
    db.execute(
        _SAVE_AB_COMPARISON,
        {
            "ab_test_id": ab_test_id,
            "recommendation": recommendation,
            "significant_metrics": significant_metrics,
            "total_metrics": total_metrics,
        },
    )
    db.commit()


# ── A/B metric deltas ─────────────────────────────────────────────────

_INSERT_AB_METRIC_DELTA = text(r"""
    INSERT INTO eval_ab_results (
        result_id, ab_test_id,
        metric_name,
        mean_a, mean_b, delta, delta_pct,
        std_a, std_b, cohens_d, effect_size,
        p_value, significant, winner,
        wins_a, wins_b, ties,
        sample_count_a, sample_count_b
    ) VALUES (
        :result_id, :ab_test_id,
        :metric_name,
        :mean_a, :mean_b, :delta, :delta_pct,
        :std_a, :std_b, :cohens_d, :effect_size,
        :p_value, :significant, :winner,
        :wins_a, :wins_b, :ties,
        :sample_count_a, :sample_count_b
    )
""")


def save_ab_metric_deltas(
    db: Session,
    *,
    ab_test_id: UUID,
    metric_deltas: list[dict],
) -> None:
    """Persist per-metric A/B comparison deltas."""
    for delta in metric_deltas:
        db.execute(
            _INSERT_AB_METRIC_DELTA,
            {
                "result_id": uuid4(),
                "ab_test_id": ab_test_id,
                **delta,
            },
        )
    db.commit()


_LIST_AB_METRIC_DELTAS = text(r"""
    SELECT
        result_id, ab_test_id,
        metric_name,
        mean_a, mean_b, delta, delta_pct,
        std_a, std_b, cohens_d, effect_size,
        p_value, significant, winner,
        wins_a, wins_b, ties,
        sample_count_a, sample_count_b,
        created_at
    FROM eval_ab_results
    WHERE ab_test_id = :ab_test_id
    ORDER BY metric_name
""")


def list_ab_metric_deltas(
    db: Session,
    *,
    ab_test_id: UUID,
) -> list[dict]:
    """Return all per-metric deltas for an A/B test."""
    rows = db.execute(
        _LIST_AB_METRIC_DELTAS, {"ab_test_id": ab_test_id}
    ).mappings().all()
    return [dict(r) for r in rows]
