"""EvalEngine — evaluation orchestration for memory/graph subsystems.

Coordinates dataset loading, engine execution, metric computation, and
result storage.  Designed to be called from API routes (``/api/v4/eval/``)
or programmatically.

Usage::

    from mneme.eval_engine import EvalEngine

    engine = EvalEngine(db_session, graph_engine)
    result = engine.run_eval("ppr_recall", params={"top_k": 12})
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from mneme.eval_engine.datasets import (
    BenchmarkDataset,
    CommunityDetectionDataset,
    GraphConnectivityDataset,
    PprRecallDataset,
    get_dataset,
    list_datasets,
)
from mneme.eval_engine.scoring import (
    AutoScorer,
    MetricRegistry,
    compute_precision_recall,
    compute_ndcg,
    compute_mrr,
    compute_graph_metrics,
)
from mneme.graph_engine.engine import GraphEngine

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Data models
# ═══════════════════════════════════════════════════════════════════════════

class EvalTaskStatus(str, Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


@dataclass
class EvalTask:
    """An evaluation task definition."""

    task_id: UUID
    task_name: str
    task_type: str
    description: str | None = None
    status: EvalTaskStatus = EvalTaskStatus.pending
    config: dict[str, Any] = field(default_factory=dict)
    total_items: int = 0
    processed_items: int = 0
    progress: float = 0.0


@dataclass
class EvalTaskResult:
    """Full result of running an evaluation task."""

    task_id: UUID
    status: EvalTaskStatus
    metric_summaries: list[dict[str, Any]] = field(default_factory=list)
    per_item_results: list[dict[str, Any]] = field(default_factory=list)
    total_items: int = 0
    processed_items: int = 0
    elapsed_ms: float = 0.0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": str(self.task_id),
            "status": self.status.value,
            "metric_summaries": self.metric_summaries,
            "per_item_results": self.per_item_results,
            "total_items": self.total_items,
            "processed_items": self.processed_items,
            "elapsed_ms": round(self.elapsed_ms, 2),
            "error": self.error,
        }


# ═══════════════════════════════════════════════════════════════════════════
# EvalEngine
# ═══════════════════════════════════════════════════════════════════════════


class EvalEngine:
    """Orchestrates evaluation runs against the memory graph.

    Parameters
    ----------
    db : Session
        Active SQLAlchemy session.
    graph_engine : GraphEngine
        Initialized GraphEngine for graph analytics.
    scorer : AutoScorer | None
        Optional pre-configured scorer (default creates one).

    Usage::

        from mneme.graph_engine import GraphEngine
        from mneme.eval_engine import EvalEngine

        graph = GraphEngine(db)
        eval_ = EvalEngine(db, graph)
        result = eval_.run_eval("ppr_recall", {"top_k": 12})
    """

    # Registry of task type → runner function
    _task_runners: dict[str, Callable] = {}

    def __init__(
        self,
        db: Session,
        graph_engine: GraphEngine,
        scorer: AutoScorer | None = None,
    ) -> None:
        self.db = db
        self.graph_engine = graph_engine
        self.scorer = scorer or AutoScorer()

    # ── Task creation ───────────────────────────────────────────────────

    def create_task(
        self,
        *,
        task_name: str,
        task_type: str,
        description: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> EvalTask:
        """Create a new evaluation task.

        Parameters
        ----------
        task_name : str
            Human-readable name.
        task_type : str
            One of the supported dataset types:
            ``"graph_connectivity"``, ``"ppr_recall"``,
            ``"community_detection"``.
        description : str | None
            Optional description.
        config : dict | None
            Task-specific configuration parameters.

        Returns
        -------
        EvalTask
        """
        if task_type not in self.available_task_types():
            raise ValueError(
                f"Unknown task_type '{task_type}'. "
                f"Available: {self.available_task_types()}"
            )

        task = EvalTask(
            task_id=uuid4(),
            task_name=task_name,
            task_type=task_type,
            description=description,
            config=config or {},
        )
        return task

    # ── Run evaluation ──────────────────────────────────────────────────

    def run_eval(
        self,
        task_type: str,
        params: dict[str, Any] | None = None,
        project_id: UUID | None = None,
    ) -> EvalTaskResult:
        """Run an evaluation and return results.

        Parameters
        ----------
        task_type : str
            Dataset type to evaluate.
        params : dict | None
            Parameters forwarded to the dataset and engine.
        project_id : UUID | None
            Scope to a project.

        Returns
        -------
        EvalTaskResult
        """
        t0 = time.monotonic()
        task_id = uuid4()
        params = params or {}

        logger.info("eval: starting %s (task_id=%s)", task_type, task_id)

        try:
            if task_type == "graph_connectivity":
                result = self._run_connectivity(task_id, params, project_id)
            elif task_type == "ppr_recall":
                result = self._run_ppr_recall(task_id, params, project_id)
            elif task_type == "community_detection":
                result = self._run_community(task_id, params, project_id)
            else:
                return EvalTaskResult(
                    task_id=task_id,
                    status=EvalTaskStatus.failed,
                    error=f"Unknown task_type: {task_type}",
                )

            result.elapsed_ms = (time.monotonic() - t0) * 1000.0
            logger.info(
                "eval: %s completed — %d/%d items, %.1fms",
                task_type, result.processed_items, result.total_items,
                result.elapsed_ms,
            )
            return result

        except Exception as exc:
            logger.exception("eval: %s failed: %s", task_type, exc)
            return EvalTaskResult(
                task_id=task_id,
                status=EvalTaskStatus.failed,
                error=str(exc),
                elapsed_ms=(time.monotonic() - t0) * 1000.0,
            )

    # ── Task runner implementations ─────────────────────────────────────

    def _run_connectivity(
        self,
        task_id: UUID,
        params: dict[str, Any],
        project_id: UUID | None,
    ) -> EvalTaskResult:
        """Evaluate shortest-path / connectivity accuracy."""
        dataset: GraphConnectivityDataset = get_dataset("graph_connectivity")
        dataset.generate(self.db, project_id=project_id)

        items = dataset.get_test_items()
        if not items:
            return EvalTaskResult(
                task_id=task_id,
                status=EvalTaskStatus.completed,
                total_items=0,
            )

        per_item: list[dict[str, Any]] = []
        metrics_collected: dict[str, list[float]] = {
            "path_accuracy": [],
            "distance_accuracy": [],
            "connectivity_accuracy": [],
        }

        for item in items:
            source_id = item["source_id"]
            target_id = item["target_id"]
            expected_connected = item.get("connected", True)
            expected_distance = item.get("expected_distance")

            # Run connectivity check
            result = self.graph_engine.connected(
                source_id=source_id,
                target_id=target_id,
                project_id=project_id,
            )

            # Score
            conn_acc = 1.0 if result.connected == expected_connected else 0.0
            dist_acc = 0.0
            if expected_distance is not None and result.shortest_distance is not None:
                if expected_distance > 0:
                    dist_acc = max(0.0, 1.0 - abs(result.shortest_distance - expected_distance) / expected_distance)
                elif result.shortest_distance == expected_distance:
                    dist_acc = 1.0

            item_result = {
                "source_id": str(source_id),
                "target_id": str(target_id),
                "connected": result.connected,
                "expected_connected": expected_connected,
                "distance": result.shortest_distance,
                "expected_distance": expected_distance,
                "path_accuracy": dist_acc,
                "connectivity_accuracy": conn_acc,
            }
            per_item.append(item_result)
            metrics_collected["path_accuracy"].append(dist_acc)
            metrics_collected["connectivity_accuracy"].append(conn_acc)

        summaries = self.scorer.summarize(metrics_collected)

        return EvalTaskResult(
            task_id=task_id,
            status=EvalTaskStatus.completed,
            metric_summaries=summaries,
            per_item_results=per_item,
            total_items=len(items),
            processed_items=len(per_item),
        )

    def _run_ppr_recall(
        self,
        task_id: UUID,
        params: dict[str, Any],
        project_id: UUID | None,
    ) -> EvalTaskResult:
        """Evaluate PPR recall against known-graph neighborhoods."""
        dataset: PprRecallDataset = get_dataset("ppr_recall")
        dataset.generate(self.db, project_id=project_id)

        items = dataset.get_test_items()
        if not items:
            return EvalTaskResult(
                task_id=task_id,
                status=EvalTaskStatus.completed,
                total_items=0,
            )

        top_k = params.get("top_k", 12)
        alpha = params.get("alpha", 0.85)

        per_item: list[dict[str, Any]] = []
        all_precision: list[float] = []
        all_recall: list[float] = []
        all_f1: list[float] = []
        all_ndcg: list[float] = []
        all_mrr: list[float] = []

        for item in items:
            seed_nodes = item["seeds"]
            expected_ids = set(item["expected_ids"])

            # Run PPR
            result = self.graph_engine.ppr(
                seed_nodes=seed_nodes,
                top_k=top_k,
                alpha=alpha,
                project_id=project_id,
            )

            predicted_ids = list(result.ppr_scores.keys())
            predicted_scores = list(result.ppr_scores.values())

            # Compute ranking metrics
            prec, rec, f1 = compute_precision_recall(
                predicted_ids, expected_ids, k=top_k,
            )
            ndcg = compute_ndcg(
                predicted_ids, expected_ids,
                scores=predicted_scores, k=top_k,
            )
            mrr = compute_mrr(
                predicted_ids, expected_ids,
            )

            all_precision.append(prec)
            all_recall.append(rec)
            all_f1.append(f1)
            all_ndcg.append(ndcg)
            all_mrr.append(mrr)

            per_item.append({
                "seeds": [str(s) for s in seed_nodes],
                "predicted": [str(p) for p in predicted_ids],
                "expected": [str(e) for e in expected_ids],
                "precision": prec,
                "recall": rec,
                "f1": f1,
                "ndcg": ndcg,
                "mrr": mrr,
            })

        summaries = self.scorer.summarize({
            "precision": all_precision,
            "recall": all_recall,
            "f1": all_f1,
            "ndcg": all_ndcg,
            "mrr": all_mrr,
        })

        return EvalTaskResult(
            task_id=task_id,
            status=EvalTaskStatus.completed,
            metric_summaries=summaries,
            per_item_results=per_item,
            total_items=len(items),
            processed_items=len(per_item),
        )

    def _run_community(
        self,
        task_id: UUID,
        params: dict[str, Any],
        project_id: UUID | None,
    ) -> EvalTaskResult:
        """Evaluate community detection quality."""
        dataset: CommunityDetectionDataset = get_dataset("community_detection")
        dataset.generate(self.db, project_id=project_id)

        items = dataset.get_test_items()
        if not items:
            return EvalTaskResult(
                task_id=task_id,
                status=EvalTaskStatus.completed,
                total_items=0,
            )

        # Run community detection
        result = self.graph_engine.community(
            project_id=project_id,
        )

        per_item: list[dict[str, Any]] = []

        if result.success and result.communities:
            # Compute graph metrics
            graph_scores = compute_graph_metrics(
                predicted=result.communities,
                ground_truth=items[0].get("ground_truth_communities", []),
            )

            per_item.append({
                "community_count": result.community_count,
                "modularity": result.modularity,
                "community_sizes": result.community_sizes,
                **graph_scores,
            })

        summaries = self.scorer.summarize({
            "modularity": [result.modularity] if result.success else [0.0],
            "community_count": [float(result.community_count)] if result.success else [0.0],
        })

        return EvalTaskResult(
            task_id=task_id,
            status=EvalTaskStatus.completed,
            metric_summaries=summaries,
            per_item_results=per_item,
            total_items=len(items),
            processed_items=len(per_item),
        )

    # ── Helpers ─────────────────────────────────────────────────────────

    @classmethod
    def available_task_types(cls) -> list[str]:
        """Return the list of supported evaluation task types."""
        return list_datasets()

    @classmethod
    def register_task_runner(
        cls,
        task_type: str,
        runner: Callable,
    ) -> None:
        """Register a custom task runner for extensibility."""
        cls._task_runners[task_type] = runner
        logger.info("Registered eval runner for '%s'", task_type)


# ═══════════════════════════════════════════════════════════════════════════
# EvalRun — multi-task evaluation orchestrator
# ═══════════════════════════════════════════════════════════════════════════


class EvalRun:
    """Orchestrate multiple evaluation tasks as a single run.

    Usage::

        run = EvalRun(eval_engine)
        run.add_task("ppr_recall", {"top_k": 12})
        run.add_task("graph_connectivity")
        results = run.execute()
    """

    def __init__(self, engine: EvalEngine) -> None:
        self.engine = engine
        self._tasks: list[tuple[str, dict[str, Any] | None]] = []

    def add_task(self, task_type: str, params: dict[str, Any] | None = None) -> EvalRun:
        """Add a task to the run."""
        self._tasks.append((task_type, params))
        return self

    def execute(
        self,
        project_id: UUID | None = None,
    ) -> dict[str, EvalTaskResult]:
        """Execute all tasks and return results keyed by task_type."""
        results: dict[str, EvalTaskResult] = {}
        for task_type, params in self._tasks:
            results[task_type] = self.engine.run_eval(
                task_type, params=params, project_id=project_id,
            )
        return results
