"""P7 Eval API — evaluation tasks, auto-scoring, A/B testing.

Powered by EvalEngine (benchmark datasets, automatic scoring, A/B comparison).

Endpoints
---------
Task Management
    ``GET    /eval/tasks``                       — list eval tasks (paginated)
    ``POST   /eval/tasks``                       — create eval task
    ``GET    /eval/tasks/{task_id}``             — eval task detail
    ``POST   /eval/tasks/{task_id}/run``         — start eval task
    ``POST   /eval/tasks/{task_id}/cancel``      — cancel eval task
    ``GET    /eval/tasks/{task_id}/results``     — eval task results

Datasets & Scoring
    ``GET    /eval/datasets``                    — list available benchmark datasets
    ``POST   /eval/run``                         — run EvalEngine evaluation
    ``POST   /eval/score``                       — compute metrics (precision, recall, NDCG, etc.)

A/B Testing
    ``GET    /eval/ab-tests``                    — list A/B tests (paginated)
    ``POST   /eval/ab-tests``                    — create A/B test
    ``GET    /eval/ab-tests/{ab_test_id}``       — A/B test detail with metric deltas
    ``POST   /eval/ab-tests/{ab_test_id}/run``   — run A/B comparison
    ``POST   /eval/ab-tests/{ab_test_id}/cancel``- cancel A/B test
    ``POST   /eval/compare``                     — ad-hoc A/B comparison (no persistence)
"""

from __future__ import annotations

import math
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends
from sqlalchemy.orm import Session

from mneme.api.context import RequestContext, get_request_context
from mneme.api.errors import ApiError
from mneme.api.schemas import envelope
from mneme.db.base import get_db
from mneme.db.eval import (
    create_ab_test,
    create_eval_task,
    get_ab_test,
    get_eval_metrics_summary,
    get_eval_task,
    list_ab_metric_deltas,
    list_ab_tests,
    list_eval_results,
    list_eval_tasks,
    save_ab_comparison,
    save_ab_metric_deltas,
    update_ab_test_status,
    update_eval_task_status,
)
from mneme.eval_engine import (
    ABTestRunner,
    ABVariant,
    AutoScorer,
    EvalEngine,
    get_dataset,
    list_datasets,
)
from mneme.eval_engine.scoring import (
    compute_bleu,
    compute_map,
    compute_mrr,
    compute_ndcg,
    compute_precision_recall,
    compute_rouge_l,
)
from mneme.graph_engine import GraphEngine
from mneme.schemas.common import PageInfo, PaginationParams
from mneme.schemas.eval import (
    ABMetricDeltaRead,
    ABTestCreate,
    ABTestDetailRead,
    ABTestListResponse,
    ABTestRead,
    ABTestStatus,
    ABVariantCreate,
    EvalMetricSummary,
    EvalResultListResponse,
    EvalResultRead,
    EvalTaskCreate,
    EvalTaskDetailRead,
    EvalTaskListResponse,
    EvalTaskRead,
    EvalTaskStatus,
)

router = APIRouter(prefix="/eval", tags=["eval"])


# ═══════════════════════════════════════════════════════════════════════════
# Dependencies
# ═══════════════════════════════════════════════════════════════════════════


def _get_graph_engine(db: Session = Depends(get_db)) -> GraphEngine:
    """Dependency injection for GraphEngine."""
    return GraphEngine(db)


def _get_eval_engine(
    db: Session = Depends(get_db),
    graph_engine: GraphEngine = Depends(_get_graph_engine),
) -> EvalEngine:
    """Dependency injection for EvalEngine."""
    return EvalEngine(db, graph_engine)


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════


def _page_info(total: int, page: int, page_size: int) -> PageInfo:
    """Build a PageInfo model for paginated responses."""
    total_pages = max(1, math.ceil(total / max(page_size, 1)))
    return PageInfo(
        page=page,
        page_size=page_size,
        total_items=total,
        total_pages=total_pages,
        has_next=page < total_pages,
        has_previous=page > 1,
    )


def _task_read(row: dict) -> EvalTaskRead:
    """Map a raw DB row dict to EvalTaskRead."""
    import json as _json
    config_raw = row.get("config_json") or {}
    if isinstance(config_raw, str):
        config_raw = _json.loads(config_raw) if config_raw.strip() else {}
    return EvalTaskRead(
        task_id=row["task_id"],
        task_name=row["task_name"],
        task_type=row["task_type"],
        description=row.get("description"),
        status=EvalTaskStatus(row["status"]),
        progress=float(row.get("progress", 0)),
        config=config_raw,
        total_items=int(row.get("total_items", 0)),
        processed_items=int(row.get("processed_items", 0)),
        created_by_user_id=row.get("created_by_user_id"),
        started_at=row.get("started_at"),
        finished_at=row.get("finished_at"),
        error_message=row.get("error_message"),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


def _ab_test_read(row: dict) -> ABTestRead:
    """Map a raw DB row dict to ABTestRead."""
    return ABTestRead(
        ab_test_id=row["ab_test_id"],
        test_name=row["test_name"],
        description=row.get("description"),
        variant_a_label=row["variant_a_label"],
        variant_b_label=row["variant_b_label"],
        task_type=row["task_type"],
        config_a=row.get("config_a_json") or {},
        config_b=row.get("config_b_json") or {},
        status=row["status"],
        recommendation=row.get("recommendation"),
        significant_metrics=int(row.get("significant_metrics", 0)),
        total_metrics=int(row.get("total_metrics", 0)),
        project_id=row.get("project_id"),
        created_by_user_id=row.get("created_by_user_id"),
        started_at=row.get("started_at"),
        finished_at=row.get("finished_at"),
        error_message=row.get("error_message"),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


def _metric_delta_read(row: dict) -> ABMetricDeltaRead:
    """Map a raw DB row dict to ABMetricDeltaRead."""
    return ABMetricDeltaRead(
        metric_name=row["metric_name"],
        mean_a=float(row.get("mean_a", 0)),
        mean_b=float(row.get("mean_b", 0)),
        delta=float(row.get("delta", 0)),
        delta_pct=float(row.get("delta_pct", 0)),
        std_a=float(row.get("std_a", 0)),
        std_b=float(row.get("std_b", 0)),
        cohens_d=float(row.get("cohens_d", 0)),
        effect_size=row.get("effect_size", ""),
        p_value=float(row["p_value"]) if row.get("p_value") is not None else None,
        significant=bool(row.get("significant", False)),
        winner=row.get("winner", ""),
        wins_a=int(row.get("wins_a", 0)),
        wins_b=int(row.get("wins_b", 0)),
        ties=int(row.get("ties", 0)),
        sample_count_a=int(row.get("sample_count_a", 0)),
        sample_count_b=int(row.get("sample_count_b", 0)),
    )


# ═══════════════════════════════════════════════════════════════════════════
# GET /eval/tasks — list eval tasks
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/tasks", response_model=dict)
def list_tasks_endpoint(
    status: str | None = None,
    task_type: str | None = None,
    pagination: PaginationParams = Depends(),
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """List evaluation tasks with optional status/type filters."""
    items, total = list_eval_tasks(
        db,
        status=status,
        task_type=task_type,
        page=pagination.page,
        page_size=pagination.page_size,
    )

    tasks = [_task_read(item) for item in items]
    pi = _page_info(total, pagination.page, pagination.page_size)
    data = EvalTaskListResponse(items=tasks, page_info=pi)

    return envelope(
        data.model_dump(mode="json"),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════
# POST /eval/tasks — create eval task
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/tasks", response_model=dict, status_code=201)
def create_task_endpoint(
    body: EvalTaskCreate,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Create a new evaluation task."""
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "Idempotency-Key header required")

    try:
        row = create_eval_task(db, payload=body)
    except Exception as exc:
        raise ApiError(500, "internal_error", f"Failed to create eval task: {exc}")

    data = _task_read(row)
    return envelope(
        data.model_dump(mode="json"),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════
# GET /eval/tasks/{task_id} — eval task detail
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/tasks/{task_id}", response_model=dict)
def get_task_endpoint(
    task_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Get evaluation task detail with metrics summary and recent results."""
    row = get_eval_task(db, task_id=task_id)
    if row is None:
        raise ApiError(404, "bad_request", f"eval task {task_id} not found")

    metrics = get_eval_metrics_summary(db, task_id=task_id)
    recent_items, _ = list_eval_results(db, task_id=task_id, page=1, page_size=5)
    recent = [
        EvalResultRead(
            result_id=r["result_id"],
            task_id=r["task_id"],
            item_index=r.get("item_index", 0),
            input=r.get("input_text"),
            expected_output=r.get("expected_output"),
            actual_output=r.get("actual_output"),
            metrics=r.get("metrics_json") or {},
            metadata_json=r.get("metadata_json") or {},
            created_at=r.get("created_at"),
        )
        for r in recent_items
    ]

    data = EvalTaskDetailRead(
        task_id=row["task_id"],
        task_name=row["task_name"],
        task_type=row["task_type"],
        description=row.get("description"),
        status=EvalTaskStatus(row["status"]),
        progress=float(row.get("progress", 0)),
        config=row.get("config_json") or {},
        total_items=int(row.get("total_items", 0)),
        processed_items=int(row.get("processed_items", 0)),
        created_by_user_id=row.get("created_by_user_id"),
        started_at=row.get("started_at"),
        finished_at=row.get("finished_at"),
        error_message=row.get("error_message"),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
        metrics_summary=[
            EvalMetricSummary.model_validate(m) for m in metrics
        ],
        recent_results=recent,
    )

    return envelope(
        data.model_dump(mode="json"),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════
# POST /eval/tasks/{task_id}/run — start eval task
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/tasks/{task_id}/run", response_model=dict)
def run_task_endpoint(
    task_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Transition an eval task from 'pending' to 'running'."""
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "Idempotency-Key header required")

    row = get_eval_task(db, task_id=task_id)
    if row is None:
        raise ApiError(404, "bad_request", f"eval task {task_id} not found")

    if row["status"] != "pending":
        raise ApiError(
            409, "bad_request",
            f"eval task is '{row['status']}', only 'pending' tasks can be run",
        )

    updated = update_eval_task_status(db, task_id=task_id, status="running")
    if updated is None:
        raise ApiError(500, "internal_error", "Failed to update eval task status")

    data = _task_read(updated)
    return envelope(
        data.model_dump(mode="json"),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════
# POST /eval/tasks/{task_id}/cancel — cancel eval task
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/tasks/{task_id}/cancel", response_model=dict)
def cancel_task_endpoint(
    task_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Cancel a running or pending eval task."""
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "Idempotency-Key header required")

    row = get_eval_task(db, task_id=task_id)
    if row is None:
        raise ApiError(404, "bad_request", f"eval task {task_id} not found")

    if row["status"] not in ("pending", "running"):
        raise ApiError(
            409, "bad_request",
            f"eval task is '{row['status']}', only pending/running tasks can be cancelled",
        )

    updated = update_eval_task_status(db, task_id=task_id, status="cancelled")
    if updated is None:
        raise ApiError(500, "internal_error", "Failed to cancel eval task")

    data = _task_read(updated)
    return envelope(
        data.model_dump(mode="json"),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════
# GET /eval/tasks/{task_id}/results — eval task results
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/tasks/{task_id}/results", response_model=dict)
def list_results_endpoint(
    task_id: UUID,
    pagination: PaginationParams = Depends(),
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """List evaluation results for a specific task."""
    task = get_eval_task(db, task_id=task_id)
    if task is None:
        raise ApiError(404, "bad_request", f"eval task {task_id} not found")

    items, total = list_eval_results(
        db,
        task_id=task_id,
        page=pagination.page,
        page_size=pagination.page_size,
    )

    results = [
        EvalResultRead(
            result_id=r["result_id"],
            task_id=r["task_id"],
            item_index=r.get("item_index", 0),
            input=r.get("input_text"),
            expected_output=r.get("expected_output"),
            actual_output=r.get("actual_output"),
            metrics=r.get("metrics_json") or {},
            metadata_json=r.get("metadata_json") or {},
            created_at=r.get("created_at"),
        )
        for r in items
    ]

    pi = _page_info(total, pagination.page, pagination.page_size)
    data = EvalResultListResponse(items=results, page_info=pi)

    return envelope(
        data.model_dump(mode="json"),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════
# GET /eval/datasets — list available benchmark datasets
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/datasets", response_model=dict)
def list_eval_datasets_endpoint(
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """List all available benchmark dataset types for evaluation.

    Returns dataset names with descriptions and configurable parameters.
    """
    datasets_info = []
    for name in list_datasets():
        ds = get_dataset(name, max_items=0)
        datasets_info.append({
            "name": ds.name,
            "description": ds.description,
            "default_max_items": ds.max_items,
        })

    return envelope(
        datasets_info,
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════
# POST /eval/run — run an EvalEngine evaluation
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/run", response_model=dict)
def run_eval_endpoint(
    task_type: str = Body(..., embed=True),
    params: dict[str, Any] = Body(default_factory=dict, embed=True),
    project_id: UUID | None = Body(default=None, embed=True),
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    eval_engine: EvalEngine = Depends(_get_eval_engine),
) -> dict:
    """Run an evaluation with auto-scoring.

    Request body (JSON)::

        {
            "task_type": "ppr_recall | graph_connectivity | community_detection",
            "params": {"top_k": 12, "alpha": 0.85},
            "project_id": "<optional uuid>"
        }

    Returns full evaluation results with metric summaries.
    """
    if not task_type:
        raise ApiError(400, "bad_request", "task_type is required")

    if task_type not in list_datasets():
        raise ApiError(
            400, "bad_request",
            f"Unknown task_type '{task_type}'. Available: {list_datasets()}",
        )

    try:
        result = eval_engine.run_eval(
            task_type=task_type,
            params=params,
            project_id=project_id,
        )
    except Exception as exc:
        raise ApiError(500, "internal_error", f"Evaluation failed: {exc}")

    return envelope(
        result.to_dict(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════
# POST /eval/score — compute auto-scoring metrics
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/score", response_model=dict)
def score_endpoint(
    predicted: list[Any] = Body(..., embed=True),
    expected: list[Any] = Body(..., embed=True),
    k: int = Body(default=10, embed=True),
    metric_type: str = Body(default="ranking", embed=True),
    scores: list[float] | None = Body(default=None, embed=True),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Compute evaluation metrics between predicted and expected results.

    Request body (JSON)::

        {
            "predicted": ["id1", "id2", "id3"],
            "expected": ["id1", "id3", "id5"],
            "k": 10,
            "metric_type": "ranking | text",
            "scores": [0.9, 0.7, 0.5]
        }

    **ranking** returns: precision@k, recall@k, f1@k, NDCG@k, MRR, MAP.
    **text** returns: BLEU, ROUGE-L, token F1 (expected as list[str]).
    """
    if not predicted or not expected:
        raise ApiError(400, "bad_request", "predicted and expected are required")

    if metric_type == "ranking":
        expected_set = set(expected)
        p, r, f1 = compute_precision_recall(predicted, expected_set, k=k)
        ndcg = compute_ndcg(predicted, expected_set, scores=scores, k=k)
        mrr = compute_mrr(predicted, expected_set)
        map_score = compute_map(predicted, expected_set)

        return envelope({
            "metric_type": "ranking",
            "k": k,
            "precision": round(p, 4),
            "recall": round(r, 4),
            "f1": round(f1, 4),
            "ndcg": round(ndcg, 4),
            "mrr": round(mrr, 4),
            "map": round(map_score, 4),
        }, request_id=context.request_id, correlation_id=context.correlation_id)

    elif metric_type == "text":
        if not isinstance(predicted[0], str) or not isinstance(expected[0], str):
            raise ApiError(400, "bad_request",
                          "For text metrics, predicted and expected must be strings")

        bleu = compute_bleu(str(predicted[0]), [str(e) for e in expected])
        rouge = compute_rouge_l(str(predicted[0]), str(expected[0]))

        return envelope({
            "metric_type": "text",
            "bleu": round(bleu, 4),
            "rouge_l": round(rouge, 4),
        }, request_id=context.request_id, correlation_id=context.correlation_id)

    else:
        raise ApiError(400, "bad_request", f"Unknown metric_type: {metric_type}")


# ═══════════════════════════════════════════════════════════════════════════
# A/B Testing Endpoints
# ═══════════════════════════════════════════════════════════════════════════


# ── GET /eval/ab-tests — list A/B tests ─────────────────────────────────

@router.get("/ab-tests", response_model=dict)
def list_ab_tests_endpoint(
    status: str | None = None,
    task_type: str | None = None,
    pagination: PaginationParams = Depends(),
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """List A/B comparison tests with optional status/type filters."""
    items, total = list_ab_tests(
        db,
        status=status,
        task_type=task_type,
        page=pagination.page,
        page_size=pagination.page_size,
    )

    tests = [_ab_test_read(item) for item in items]
    pi = _page_info(total, pagination.page, pagination.page_size)
    data = ABTestListResponse(items=tests, page_info=pi)

    return envelope(
        data.model_dump(mode="json"),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ── POST /eval/ab-tests — create A/B test ───────────────────────────────

@router.post("/ab-tests", response_model=dict, status_code=201)
def create_ab_test_endpoint(
    body: ABTestCreate,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Create a new A/B comparison test.

    Request body (JSON)::

        {
            "test_name": "PPR top-k comparison",
            "description": "Compare PPR recall with top_k=12 vs top_k=20",
            "variant_a": {
                "label": "baseline_k12",
                "task_type": "ppr_recall",
                "params": {"top_k": 12, "alpha": 0.85}
            },
            "variant_b": {
                "label": "experimental_k20",
                "task_type": "ppr_recall",
                "params": {"top_k": 20, "alpha": 0.85}
            },
            "project_id": "<optional uuid>"
        }

    Both variants must use the same task_type.
    """
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "Idempotency-Key header required")

    if body.variant_a.task_type != body.variant_b.task_type:
        raise ApiError(
            400, "bad_request",
            f"Both variants must use the same task_type. "
            f"Got '{body.variant_a.task_type}' vs '{body.variant_b.task_type}'",
        )

    if body.variant_a.task_type not in list_datasets():
        raise ApiError(
            400, "bad_request",
            f"Unknown task_type '{body.variant_a.task_type}'. "
            f"Available: {list_datasets()}",
        )

    try:
        row = create_ab_test(db, payload=body)
    except Exception as exc:
        raise ApiError(500, "internal_error", f"Failed to create A/B test: {exc}")

    data = _ab_test_read(row)
    return envelope(
        data.model_dump(mode="json"),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ── GET /eval/ab-tests/{ab_test_id} — A/B test detail ──────────────────

@router.get("/ab-tests/{ab_test_id}", response_model=dict)
def get_ab_test_endpoint(
    ab_test_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Get A/B test detail with per-metric comparison deltas."""
    row = get_ab_test(db, ab_test_id=ab_test_id)
    if row is None:
        raise ApiError(404, "bad_request", f"A/B test {ab_test_id} not found")

    # Fetch metric deltas if completed
    metric_deltas: list[ABMetricDeltaRead] = []
    elapsed_ms_a = 0.0
    elapsed_ms_b = 0.0
    sample_count = 0

    if row["status"] == "completed":
        deltas = list_ab_metric_deltas(db, ab_test_id=ab_test_id)
        metric_deltas = [_metric_delta_read(d) for d in deltas]
        if deltas:
            sample_count = max(
                deltas[0].get("sample_count_a", 0),
                deltas[0].get("sample_count_b", 0),
            )

    data = ABTestDetailRead(
        ab_test_id=row["ab_test_id"],
        test_name=row["test_name"],
        description=row.get("description"),
        variant_a_label=row["variant_a_label"],
        variant_b_label=row["variant_b_label"],
        task_type=row["task_type"],
        config_a=row.get("config_a_json") or {},
        config_b=row.get("config_b_json") or {},
        status=row["status"],
        recommendation=row.get("recommendation"),
        significant_metrics=int(row.get("significant_metrics", 0)),
        total_metrics=int(row.get("total_metrics", 0)),
        project_id=row.get("project_id"),
        created_by_user_id=row.get("created_by_user_id"),
        started_at=row.get("started_at"),
        finished_at=row.get("finished_at"),
        error_message=row.get("error_message"),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
        metric_deltas=metric_deltas,
        elapsed_ms_a=elapsed_ms_a,
        elapsed_ms_b=elapsed_ms_b,
        sample_count=sample_count,
    )

    return envelope(
        data.model_dump(mode="json"),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ── POST /eval/ab-tests/{ab_test_id}/run — run A/B test ────────────────

@router.post("/ab-tests/{ab_test_id}/run", response_model=dict)
def run_ab_test_endpoint(
    ab_test_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    eval_engine: EvalEngine = Depends(_get_eval_engine),
) -> dict:
    """Execute an A/B comparison test.

    Runs both variants against the same benchmark dataset and persists
    per-metric comparison deltas with statistical significance tests.
    """
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "Idempotency-Key header required")

    row = get_ab_test(db, ab_test_id=ab_test_id)
    if row is None:
        raise ApiError(404, "bad_request", f"A/B test {ab_test_id} not found")

    if row["status"] != "pending":
        raise ApiError(
            409, "bad_request",
            f"A/B test is '{row['status']}', only 'pending' tests can be run",
        )

    # Transition to running
    updated = update_ab_test_status(db, ab_test_id=ab_test_id, status="running")
    if updated is None:
        raise ApiError(500, "internal_error", "Failed to update A/B test status")

    # Build variants
    variant_a = ABVariant(
        label=row["variant_a_label"],
        task_type=row["task_type"],
        params=row.get("config_a_json") or {},
    )
    variant_b = ABVariant(
        label=row["variant_b_label"],
        task_type=row["task_type"],
        params=row.get("config_b_json") or {},
    )

    # Run comparison
    runner = ABTestRunner(eval_engine)
    project_id = row.get("project_id")

    try:
        comparison = runner.run(
            variant_a=variant_a,
            variant_b=variant_b,
            project_id=project_id,
        )
    except Exception as exc:
        update_ab_test_status(
            db, ab_test_id=ab_test_id, status="failed", error_message=str(exc),
        )
        raise ApiError(500, "internal_error", f"A/B test execution failed: {exc}")

    if comparison.error:
        update_ab_test_status(
            db, ab_test_id=ab_test_id, status="failed",
            error_message=comparison.error,
        )
        raise ApiError(500, "internal_error", f"A/B test failed: {comparison.error}")

    # Count significant metrics
    significant = sum(1 for d in comparison.metric_deltas if d.significant)

    # Persist comparison results
    save_ab_comparison(
        db,
        ab_test_id=ab_test_id,
        recommendation=comparison.recommendation,
        significant_metrics=significant,
        total_metrics=len(comparison.metric_deltas),
    )

    save_ab_metric_deltas(
        db,
        ab_test_id=ab_test_id,
        metric_deltas=[d.to_dict() for d in comparison.metric_deltas],
    )

    # Return full result
    row_after = get_ab_test(db, ab_test_id=ab_test_id)
    data = _ab_test_read(row_after) if row_after else _ab_test_read(row)

    return envelope(
        {
            **data.model_dump(mode="json"),
            "metric_deltas": [d.to_dict() for d in comparison.metric_deltas],
            "elapsed_ms_a": comparison.elapsed_ms_a,
            "elapsed_ms_b": comparison.elapsed_ms_b,
            "sample_count": comparison.sample_count,
        },
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ── POST /eval/ab-tests/{ab_test_id}/cancel — cancel A/B test ──────────

@router.post("/ab-tests/{ab_test_id}/cancel", response_model=dict)
def cancel_ab_test_endpoint(
    ab_test_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Cancel a running or pending A/B test."""
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "Idempotency-Key header required")

    row = get_ab_test(db, ab_test_id=ab_test_id)
    if row is None:
        raise ApiError(404, "bad_request", f"A/B test {ab_test_id} not found")

    if row["status"] not in ("pending", "running"):
        raise ApiError(
            409, "bad_request",
            f"A/B test is '{row['status']}', only pending/running tests can be cancelled",
        )

    updated = update_ab_test_status(db, ab_test_id=ab_test_id, status="cancelled")
    if updated is None:
        raise ApiError(500, "internal_error", "Failed to cancel A/B test")

    data = _ab_test_read(updated)
    return envelope(
        data.model_dump(mode="json"),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ── POST /eval/compare — ad-hoc A/B comparison (no persistence) ────────

@router.post("/compare", response_model=dict)
def compare_endpoint(
    variant_a: dict[str, Any] = Body(..., embed=True),
    variant_b: dict[str, Any] = Body(..., embed=True),
    project_id: UUID | None = Body(default=None, embed=True),
    context: RequestContext = Depends(get_request_context),
    eval_engine: EvalEngine = Depends(_get_eval_engine),
) -> dict:
    """Run an ad-hoc A/B comparison without persisting to the database.

    Request body (JSON)::

        {
            "variant_a": {
                "label": "baseline",
                "task_type": "ppr_recall",
                "params": {"top_k": 12, "alpha": 0.85}
            },
            "variant_b": {
                "label": "experiment",
                "task_type": "ppr_recall",
                "params": {"top_k": 20, "alpha": 0.90}
            },
            "project_id": "<optional uuid>"
        }

    Returns full A/B comparison with per-metric deltas, Cohen's d,
    p-values, and recommendation.
    """
    # Validate variant A
    label_a = variant_a.get("label", "A")
    task_a = variant_a.get("task_type")
    params_a = variant_a.get("params", {})

    if not task_a:
        raise ApiError(400, "bad_request", "variant_a.task_type is required")

    # Validate variant B
    label_b = variant_b.get("label", "B")
    task_b = variant_b.get("task_type")
    params_b = variant_b.get("params", {})

    if not task_b:
        raise ApiError(400, "bad_request", "variant_b.task_type is required")

    if task_a != task_b:
        raise ApiError(
            400, "bad_request",
            f"Both variants must use the same task_type. "
            f"Got '{task_a}' vs '{task_b}'",
        )

    if task_a not in list_datasets():
        raise ApiError(
            400, "bad_request",
            f"Unknown task_type '{task_a}'. Available: {list_datasets()}",
        )

    runner = ABTestRunner(eval_engine)

    try:
        comparison = runner.run(
            variant_a=ABVariant(label=label_a, task_type=task_a, params=params_a),
            variant_b=ABVariant(label=label_b, task_type=task_a, params=params_b),
            project_id=project_id,
        )
    except Exception as exc:
        raise ApiError(500, "internal_error", f"A/B comparison failed: {exc}")

    if comparison.error:
        raise ApiError(500, "internal_error", f"A/B comparison failed: {comparison.error}")

    return envelope(
        comparison.to_dict(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )
