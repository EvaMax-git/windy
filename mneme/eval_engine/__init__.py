"""EvalEngine — benchmark datasets, automatic scoring, evaluation orchestration.

Public API
----------
* ``EvalEngine`` — orchestrator class for running evaluations
* ``BenchmarkDataset`` — built-in benchmark dataset builders
* ``AutoScorer`` — automatic metric computation (precision/recall, NDCG, MRR, BLEU, ROUGE)
* ``EvalRun`` — evaluation run orchestrator
* ``MetricRegistry`` — extensible metric registry
* ``ABTestRunner`` — A/B comparison runner for evaluating config/model variants
* ``ABVariant`` — single variant definition (task_type + params)
* ``ABResult`` — per-metric A/B comparison result with significance

Datasets
--------
* ``graph_connectivity`` — tests shortest-path accuracy
* ``ppr_recall`` — tests PPR recall against known-graph neighborhoods
* ``community_detection`` — tests community quality against ground truth

Metrics
-------
* ``precision@k`` / ``recall@k`` / ``f1@k`` — ranking metrics
* ``ndcg@k`` — Normalized Discounted Cumulative Gain
* ``mrr`` — Mean Reciprocal Rank
* ``map`` — Mean Average Precision
* ``bleu`` / ``rouge_l`` — text quality metrics

A/B Testing
-----------
* ``ABTestRunner`` — runs A/B comparison on a shared dataset
* ``ABVariant`` — describes one branch (task_type + parameter overrides)
* ``ABComparison`` — per-metric delta, Cohen's d, win/tie/loss counts
"""

from mneme.eval_engine.engine import (
    EvalEngine,
    EvalRun,
    EvalTask,
    EvalTaskResult,
)
from mneme.eval_engine.datasets import (
    BenchmarkDataset,
    GraphConnectivityDataset,
    PprRecallDataset,
    CommunityDetectionDataset,
    get_dataset,
    list_datasets,
)
from mneme.eval_engine.scoring import (
    AutoScorer,
    MetricRegistry,
    compute_precision_recall,
    compute_ndcg,
    compute_mrr,
    compute_map,
    compute_bleu,
    compute_rouge_l,
    compute_token_f1,
    compute_graph_metrics,
)
from mneme.eval_engine.ab_testing import (
    ABTestRunner,
    ABVariant,
    ABComparison,
    ABTestConfig,
)

__all__ = [
    # ── Engine ──
    "EvalEngine",
    "EvalRun",
    "EvalTask",
    "EvalTaskResult",
    # ── Datasets ──
    "BenchmarkDataset",
    "GraphConnectivityDataset",
    "PprRecallDataset",
    "CommunityDetectionDataset",
    "get_dataset",
    "list_datasets",
    # ── Scoring ──
    "AutoScorer",
    "MetricRegistry",
    "compute_precision_recall",
    "compute_ndcg",
    "compute_mrr",
    "compute_map",
    "compute_bleu",
    "compute_rouge_l",
    "compute_token_f1",
    "compute_graph_metrics",
    # ── A/B Testing ──
    "ABTestRunner",
    "ABVariant",
    "ABComparison",
    "ABTestConfig",
]
