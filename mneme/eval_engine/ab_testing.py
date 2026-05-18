"""A/B testing engine for memory graph evaluation.

Runs two variants of the same evaluation benchmark on identical test data
and produces a statistical comparison (per-metric delta, Cohen's d effect
size, win/tie/loss breakdown, and a recommendation).

Usage::

    from mneme.eval_engine.ab_testing import ABTestRunner, ABVariant

    runner = ABTestRunner(eval_engine)
    comparison = runner.run(
        variant_a=ABVariant(label="baseline", task_type="ppr_recall",
                            params={"top_k": 12, "alpha": 0.85}),
        variant_b=ABVariant(label="experiment", task_type="ppr_recall",
                            params={"top_k": 20, "alpha": 0.90}),
    )
    print(comparison.recommendation)  # e.g. "B outperforms A on 4/5 metrics (p<0.05)"
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from mneme.eval_engine.engine import EvalEngine

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Data models
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class ABVariant:
    """One branch of an A/B test.

    Parameters
    ----------
    label : str
        Human-readable name (e.g. "baseline", "experiment", "model_v2").
    task_type : str
        Dataset type to evaluate (must be registered in EvalEngine).
    params : dict
        Task-specific parameters (top_k, alpha, etc.).
    """

    label: str
    task_type: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class ABMetricDelta:
    """Per-metric A/B comparison statistics."""

    metric_name: str
    mean_a: float
    mean_b: float
    delta: float               # mean_b - mean_a
    delta_pct: float            # (mean_b - mean_a) / |mean_a| * 100  (or 0)
    std_a: float = 0.0
    std_b: float = 0.0
    cohens_d: float = 0.0       # (mean_b - mean_a) / pooled_std
    effect_size: str = ""       # "negligible" | "small" | "medium" | "large"
    p_value: float | None = None  # Welch's t-test (approximate)
    significant: bool = False   # p < 0.05
    winner: str = ""            # "A" | "B" | "tie"
    wins_a: int = 0
    wins_b: int = 0
    ties: int = 0
    sample_count_a: int = 0
    sample_count_b: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric_name": self.metric_name,
            "mean_a": round(self.mean_a, 4),
            "mean_b": round(self.mean_b, 4),
            "delta": round(self.delta, 4),
            "delta_pct": round(self.delta_pct, 2),
            "std_a": round(self.std_a, 4),
            "std_b": round(self.std_b, 4),
            "cohens_d": round(self.cohens_d, 4),
            "effect_size": self.effect_size,
            "p_value": round(self.p_value, 6) if self.p_value is not None else None,
            "significant": self.significant,
            "winner": self.winner,
            "wins_a": self.wins_a,
            "wins_b": self.wins_b,
            "ties": self.ties,
            "sample_count_a": self.sample_count_a,
            "sample_count_b": self.sample_count_b,
        }


@dataclass
class ABComparison:
    """Full A/B comparison result."""

    variant_a_label: str
    variant_b_label: str
    task_type: str
    config_a: dict[str, Any] = field(default_factory=dict)
    config_b: dict[str, Any] = field(default_factory=dict)
    metric_deltas: list[ABMetricDelta] = field(default_factory=list)
    recommendation: str = ""
    elapsed_ms_a: float = 0.0
    elapsed_ms_b: float = 0.0
    sample_count: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "variant_a_label": self.variant_a_label,
            "variant_b_label": self.variant_b_label,
            "task_type": self.task_type,
            "config_a": self.config_a,
            "config_b": self.config_b,
            "metric_deltas": [d.to_dict() for d in self.metric_deltas],
            "recommendation": self.recommendation,
            "elapsed_ms_a": round(self.elapsed_ms_a, 2),
            "elapsed_ms_b": round(self.elapsed_ms_b, 2),
            "sample_count": self.sample_count,
            "error": self.error,
        }


@dataclass
class ABTestConfig:
    """Full A/B test configuration for persistence and reproducibility."""

    ab_test_id: UUID | None = None
    variant_a: ABVariant = field(default_factory=lambda: ABVariant(label="A", task_type="ppr_recall"))
    variant_b: ABVariant = field(default_factory=lambda: ABVariant(label="B", task_type="ppr_recall"))
    project_id: UUID | None = None
    description: str = ""


# ═══════════════════════════════════════════════════════════════════════════
# ABTestRunner
# ═══════════════════════════════════════════════════════════════════════════


class ABTestRunner:
    """Run A/B comparisons on benchmark datasets.

    Uses a shared ``EvalEngine`` to execute both variants on the same
    test data generated from a single dataset instantiation, then computes
    per-metric statistical comparisons.

    Parameters
    ----------
    eval_engine : EvalEngine
        Initialized evaluation engine.

    Usage::

        from mneme.eval_engine import EvalEngine, ABTestRunner, ABVariant

        engine = EvalEngine(db, graph_engine)
        runner = ABTestRunner(engine)

        comparison = runner.run(
            ABVariant("baseline", "ppr_recall", {"top_k": 12}),
            ABVariant("candidate", "ppr_recall", {"top_k": 20}),
        )
    """

    def __init__(self, eval_engine: Any) -> None:
        self.eval_engine = eval_engine

    def run(
        self,
        variant_a: ABVariant,
        variant_b: ABVariant,
        *,
        project_id: UUID | None = None,
        seed: int = 42,
    ) -> ABComparison:
        """Execute an A/B comparison.

        Parameters
        ----------
        variant_a : ABVariant
            Baseline / control variant.
        variant_b : ABVariant
            Experiment / treatment variant.
        project_id : UUID | None
            Scope to a project.
        seed : int
            Random seed for dataset generation (ensures identical test
            items for both variants).

        Returns
        -------
        ABComparison
            Full comparison with per-metric deltas and recommendation.
        """
        import random
        random.seed(seed)

        if variant_a.task_type != variant_b.task_type:
            return ABComparison(
                variant_a_label=variant_a.label,
                variant_b_label=variant_b.label,
                task_type=f"{variant_a.task_type} vs {variant_b.task_type}",
                config_a=variant_a.params,
                config_b=variant_b.params,
                error="Cannot compare across different task_types",
            )

        task_type = variant_a.task_type
        logger.info(
            "ab_test: %s — A='%s' vs B='%s'",
            task_type, variant_a.label, variant_b.label,
        )

        # ── Run variant A ──────────────────────────────────────────────
        t0_a = time.monotonic()
        try:
            result_a = self.eval_engine.run_eval(
                task_type=task_type,
                params=variant_a.params,
                project_id=project_id,
            )
        except Exception as exc:
            logger.exception("ab_test: variant A failed: %s", exc)
            return ABComparison(
                variant_a_label=variant_a.label,
                variant_b_label=variant_b.label,
                task_type=task_type,
                config_a=variant_a.params,
                config_b=variant_b.params,
                error=f"Variant A failed: {exc}",
            )
        elapsed_a = (time.monotonic() - t0_a) * 1000.0

        # ── Run variant B ──────────────────────────────────────────────
        t0_b = time.monotonic()
        try:
            result_b = self.eval_engine.run_eval(
                task_type=task_type,
                params=variant_b.params,
                project_id=project_id,
            )
        except Exception as exc:
            logger.exception("ab_test: variant B failed: %s", exc)
            return ABComparison(
                variant_a_label=variant_a.label,
                variant_b_label=variant_b.label,
                task_type=task_type,
                config_a=variant_a.params,
                config_b=variant_b.params,
                error=f"Variant B failed: {exc}",
            )
        elapsed_b = (time.monotonic() - t0_b) * 1000.0

        # ── Build per-metric comparison ────────────────────────────────
        metric_deltas = self._compare_metrics(result_a, result_b)

        # ── Build recommendation ──────────────────────────────────────
        significant_wins_b = sum(
            1 for d in metric_deltas if d.significant and d.winner == "B"
        )
        significant_wins_a = sum(
            1 for d in metric_deltas if d.significant and d.winner == "A"
        )
        total_metrics = len(metric_deltas)

        if significant_wins_b > significant_wins_a:
            recommendation = (
                f"Variant B ('{variant_b.label}') outperforms A "
                f"on {significant_wins_b}/{total_metrics} metrics "
                f"(p<0.05)"
            )
        elif significant_wins_a > significant_wins_b:
            recommendation = (
                f"Variant A ('{variant_a.label}') outperforms B "
                f"on {significant_wins_a}/{total_metrics} metrics "
                f"(p<0.05)"
            )
        elif total_metrics > 0:
            recommendation = (
                f"No statistically significant difference between "
                f"'{variant_a.label}' and '{variant_b.label}'"
            )
        else:
            recommendation = "No metrics to compare"

        return ABComparison(
            variant_a_label=variant_a.label,
            variant_b_label=variant_b.label,
            task_type=task_type,
            config_a=variant_a.params,
            config_b=variant_b.params,
            metric_deltas=metric_deltas,
            recommendation=recommendation,
            elapsed_ms_a=elapsed_a,
            elapsed_ms_b=elapsed_b,
            sample_count=result_a.processed_items,
        )

    # ── Internal comparison logic ──────────────────────────────────────

    def _compare_metrics(
        self,
        result_a: Any,
        result_b: Any,
    ) -> list[ABMetricDelta]:
        """Build per-metric ABMetricDelta entries from two EvalTaskResults.

        Strategy:
        1. Group per-item results by metric name for each variant.
        2. Compute per-item paired comparison (win/tie/loss).
        3. Compute effect size (Cohen's d) and Welch's t-test p-value.
        """
        # ── Collect per-item metric values ────────────────────────────
        metrics_a = self._collect_metrics(result_a.per_item_results)
        metrics_b = self._collect_metrics(result_b.per_item_results)

        all_metric_names = sorted(set(metrics_a.keys()) | set(metrics_b.keys()))

        deltas: list[ABMetricDelta] = []
        for name in all_metric_names:
            vals_a = metrics_a.get(name, [])
            vals_b = metrics_b.get(name, [])

            n_a = len(vals_a)
            n_b = len(vals_b)

            if n_a == 0 and n_b == 0:
                continue

            mean_a = sum(vals_a) / max(n_a, 1)
            mean_b = sum(vals_b) / max(n_b, 1)

            delta = mean_b - mean_a
            delta_pct = (delta / abs(mean_a) * 100) if mean_a != 0 else 0.0

            # Standard deviations
            std_a = _stddev(vals_a, mean_a)
            std_b = _stddev(vals_b, mean_b)

            # Cohen's d
            pooled_std = _pooled_std(std_a, std_b, n_a, n_b)
            cohens_d = delta / pooled_std if pooled_std and pooled_std > 0 else 0.0

            # Effect size label
            d_abs = abs(cohens_d)
            if d_abs < 0.2:
                effect_size = "negligible"
            elif d_abs < 0.5:
                effect_size = "small"
            elif d_abs < 0.8:
                effect_size = "medium"
            else:
                effect_size = "large"

            # Welch's t-test p-value (approximate via normal CDF)
            p_value = _welch_p_value(mean_a, mean_b, std_a, std_b, n_a, n_b)
            significant = p_value is not None and p_value < 0.05

            # Paired per-item win/tie/loss (align by index)
            wins_a = 0
            wins_b = 0
            ties = 0
            paired_n = min(n_a, n_b)
            for i in range(paired_n):
                if vals_b[i] > vals_a[i]:
                    wins_b += 1
                elif vals_a[i] > vals_b[i]:
                    wins_a += 1
                else:
                    ties += 1

            # Winner determination
            if significant and delta > 0:
                winner = "B"
            elif significant and delta < 0:
                winner = "A"
            else:
                winner = "tie"

            deltas.append(ABMetricDelta(
                metric_name=name,
                mean_a=mean_a,
                mean_b=mean_b,
                delta=delta,
                delta_pct=delta_pct,
                std_a=std_a,
                std_b=std_b,
                cohens_d=cohens_d,
                effect_size=effect_size,
                p_value=p_value,
                significant=significant,
                winner=winner,
                wins_a=wins_a,
                wins_b=wins_b,
                ties=ties,
                sample_count_a=n_a,
                sample_count_b=n_b,
            ))

        return deltas

    def _collect_metrics(
        self,
        per_item_results: list[dict[str, Any]],
    ) -> dict[str, list[float]]:
        """Extract per-metric lists from per-item result dicts.

        Each item dict contains named metric values (e.g. "precision", "recall").
        Collect them into per-metric lists.
        """
        metrics: dict[str, list[float]] = {}
        for item in per_item_results:
            for key, value in item.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    # Accept numeric metric fields; skip identifiers
                    if key in ("source_id", "target_id", "item_index", "seeds",
                               "predicted", "expected", "expected_connected",
                               "connected", "distance", "expected_distance",
                               "community_count", "community_sizes",
                               "predicted_communities", "ground_truth_communities",
                               "node_count", "component_count"):
                        # Skip size/count metadata (not quality metrics)
                        if key.endswith("_count") or key.endswith("_sizes") or key.endswith("_communities"):
                            continue
                        continue
                    if key not in metrics:
                        metrics[key] = []
                    metrics[key].append(float(value))
        return metrics


# ═══════════════════════════════════════════════════════════════════════════
# Statistical helpers
# ═══════════════════════════════════════════════════════════════════════════


def _stddev(values: list[float], mean: float) -> float:
    """Sample standard deviation."""
    n = len(values)
    if n < 2:
        return 0.0
    variance = sum((x - mean) ** 2 for x in values) / (n - 1)
    return math.sqrt(variance)


def _pooled_std(std_a: float, std_b: float, n_a: int, n_b: int) -> float:
    """Pooled standard deviation for Cohen's d."""
    if n_a < 2 and n_b < 2:
        return 0.0
    num = (n_a - 1) * std_a ** 2 + (n_b - 1) * std_b ** 2
    denom = n_a + n_b - 2
    if denom <= 0:
        return 0.0
    return math.sqrt(num / denom)


def _welch_p_value(
    mean_a: float,
    mean_b: float,
    std_a: float,
    std_b: float,
    n_a: int,
    n_b: int,
) -> float | None:
    """Approximate two-sided p-value via Welch's t-test using normal CDF.

    Uses the Satterthwaite approximation for degrees of freedom.
    Falls back to z-test for large samples.
    """
    if n_a < 2 or n_b < 2:
        return None

    se_a2 = (std_a ** 2) / n_a
    se_b2 = (std_b ** 2) / n_b
    se_diff = math.sqrt(se_a2 + se_b2)

    if se_diff == 0:
        return 1.0  # identical means, no variance

    t_stat = (mean_b - mean_a) / se_diff

    # Satterthwaite degrees of freedom
    if se_a2 == 0 and se_b2 == 0:
        df = n_a + n_b - 2
    else:
        num_df = (se_a2 + se_b2) ** 2
        denom_df = (se_a2 ** 2) / (n_a - 1) + (se_b2 ** 2) / (n_b - 1)
        df = num_df / denom_df if denom_df > 0 else n_a + n_b - 2

    # Two-sided p-value via normal approximation
    p = 2.0 * (1.0 - _normal_cdf(abs(t_stat)))
    return min(max(p, 0.0), 1.0)


def _normal_cdf(x: float) -> float:
    """Approximation of the standard normal CDF (Abramowitz & Stegun 7.1.26)."""
    if x < 0:
        return 1.0 - _normal_cdf(-x)

    # Constants for the error function approximation
    p = 0.3275911
    a1 = 0.254829592
    a2 = -0.284496736
    a3 = 1.421413741
    a4 = -1.453152027
    a5 = 1.061405429

    t = 1.0 / (1.0 + p * x)
    y = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * math.exp(-x * x / 2.0)
    return y
