"""P7 Eval API schemas — evaluation tasks, results, metric summaries, and A/B tests.

Tables
------
* ``eval_tasks`` — evaluation tasks (precision/recall, BLEU, ROUGE, etc.)
* ``eval_results`` — per-item evaluation results with metric scores
* ``eval_ab_tests`` — A/B comparison test definitions
* ``eval_ab_results`` — per-metric A/B comparison results
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import Field

from mneme.schemas.common import ApiSchema, PaginatedData


# ── Enums ────────────────────────────────────────────────────────────────

class EvalTaskStatus(str, Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class ABTestStatus(str, Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


# ── Eval Task ────────────────────────────────────────────────────────────

class EvalTaskRead(ApiSchema):
    """Evaluation task summary returned from list/detail endpoints."""

    task_id: UUID
    task_name: str
    task_type: str
    description: str | None = None
    status: EvalTaskStatus = EvalTaskStatus.pending
    progress: float = 0.0
    config: dict[str, Any] = Field(default_factory=dict)
    total_items: int = 0
    processed_items: int = 0
    created_by_user_id: UUID | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_message: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class EvalTaskListResponse(PaginatedData[EvalTaskRead]):
    """Paginated list of eval tasks."""
    pass


class EvalTaskCreate(ApiSchema):
    """Create a new eval task."""

    task_name: str = Field(min_length=1, max_length=200)
    task_type: str = "precision_recall"
    description: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)


# ── Eval Metric Summary ─────────────────────────────────────────────────

class EvalMetricSummary(ApiSchema):
    """Aggregated metric summary for an eval task."""

    metric_name: str
    aggregation: str = "mean"
    value: float = 0.0
    min_value: float = 0.0
    max_value: float = 0.0
    std_dev: float = 0.0
    sample_count: int = 0


# ── Eval Task Detail ────────────────────────────────────────────────────

class EvalTaskDetailRead(EvalTaskRead):
    """Extended eval task with metrics summary."""

    metrics_summary: list[EvalMetricSummary] = Field(default_factory=list)
    recent_results: list["EvalResultRead"] = Field(default_factory=list)


# ── Eval Result ──────────────────────────────────────────────────────────

class EvalResultRead(ApiSchema):
    """A single evaluation result item."""

    result_id: UUID
    task_id: UUID
    item_index: int = 0
    input: str | None = None
    expected_output: str | None = None
    actual_output: str | None = None
    metrics: dict[str, float] = Field(default_factory=dict)
    metadata_json: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None


class EvalResultListResponse(PaginatedData[EvalResultRead]):
    """Paginated list of eval results."""
    pass


# ── A/B Test ─────────────────────────────────────────────────────────────

class ABVariantCreate(ApiSchema):
    """Definition of one A/B test branch."""

    label: str = Field(min_length=1, max_length=100)
    task_type: str = Field(min_length=1, max_length=80)
    params: dict[str, Any] = Field(default_factory=dict)


class ABTestCreate(ApiSchema):
    """Create a new A/B comparison test."""

    test_name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    variant_a: ABVariantCreate
    variant_b: ABVariantCreate
    project_id: UUID | None = None


class ABTestRead(ApiSchema):
    """A/B test summary returned from list/detail endpoints."""

    ab_test_id: UUID
    test_name: str
    description: str | None = None
    variant_a_label: str
    variant_b_label: str
    task_type: str
    config_a: dict[str, Any] = Field(default_factory=dict)
    config_b: dict[str, Any] = Field(default_factory=dict)
    status: str = "pending"
    recommendation: str | None = None
    significant_metrics: int = 0
    total_metrics: int = 0
    project_id: UUID | None = None
    created_by_user_id: UUID | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_message: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ABTestListResponse(PaginatedData[ABTestRead]):
    """Paginated list of A/B tests."""
    pass


class ABMetricDeltaRead(ApiSchema):
    """Per-metric A/B comparison delta."""

    metric_name: str
    mean_a: float = 0.0
    mean_b: float = 0.0
    delta: float = 0.0
    delta_pct: float = 0.0
    std_a: float = 0.0
    std_b: float = 0.0
    cohens_d: float = 0.0
    effect_size: str = ""
    p_value: float | None = None
    significant: bool = False
    winner: str = ""
    wins_a: int = 0
    wins_b: int = 0
    ties: int = 0
    sample_count_a: int = 0
    sample_count_b: int = 0


class ABTestDetailRead(ABTestRead):
    """Extended A/B test with metric deltas."""

    metric_deltas: list[ABMetricDeltaRead] = Field(default_factory=list)
    elapsed_ms_a: float = 0.0
    elapsed_ms_b: float = 0.0
    sample_count: int = 0
