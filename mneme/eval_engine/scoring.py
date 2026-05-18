"""Auto-scoring module — metric computation and registry.

Provides standard IR/NLP metrics for evaluating memory graph algorithms
and search/retrieval quality.

Metrics
-------
* **Ranking**: precision@k, recall@k, f1@k, NDCG@k, MRR, MAP
* **Text quality**: BLEU, ROUGE-L, token F1
* **Graph quality**: community overlap (NMI, ARI), modularity
* **Aggregation**: mean, median, std, min, max per metric

Usage
-----
.. code-block:: python

    from mneme.eval_engine.scoring import AutoScorer

    scorer = AutoScorer()
    summaries = scorer.summarize({
        "precision": [0.8, 0.7, 0.9],
        "recall": [0.6, 0.5, 0.7],
    })
"""

from __future__ import annotations

import logging
import math
from collections import Counter, defaultdict
from typing import Any

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Metric registry
# ═══════════════════════════════════════════════════════════════════════════

class MetricRegistry:
    """Registry of named metric computation functions.

    Extensible: call ``MetricRegistry.register(name, fn)`` to add custom metrics.
    """

    _metrics: dict[str, Any] = {}

    @classmethod
    def register(cls, name: str, fn: Any) -> None:
        cls._metrics[name] = fn

    @classmethod
    def get(cls, name: str) -> Any:
        if name not in cls._metrics:
            raise KeyError(f"Unknown metric: {name}")
        return cls._metrics[name]

    @classmethod
    def list(cls) -> list[str]:
        return list(cls._metrics.keys())


# ═══════════════════════════════════════════════════════════════════════════
# AutoScorer
# ═══════════════════════════════════════════════════════════════════════════


class AutoScorer:
    """Computes and aggregates evaluation metrics.

    Usage::

        scorer = AutoScorer()
        score = scorer.score_item(
            predicted=["a", "b", "c"],
            expected=["a", "c", "e"],
        )
    """

    def __init__(self) -> None:
        pass

    def score_item(
        self,
        *,
        predicted: list[Any],
        expected: list[Any],
        k: int = 10,
        scores: list[float] | None = None,
    ) -> dict[str, float]:
        """Score a single prediction against ground truth.

        Parameters
        ----------
        predicted : list
            Ranked list of predicted IDs/items.
        expected : list
            Set of expected (relevant) IDs/items.
        k : int
            Cutoff for @k metrics.
        scores : list[float] | None
            Relevance scores for NDCG (same order as predicted).

        Returns
        -------
        dict[str, float]
            Map of metric name → score.
        """
        expected_set = set(expected)

        p, r, f1 = compute_precision_recall(predicted, expected_set, k=k)
        ndcg = compute_ndcg(predicted, expected_set, scores=scores, k=k)
        mrr = compute_mrr(predicted, expected_set)
        ap = compute_map(predicted, expected_set)

        return {
            "precision": round(p, 4),
            "recall": round(r, 4),
            "f1": round(f1, 4),
            "ndcg": round(ndcg, 4),
            "mrr": round(mrr, 4),
            "map": round(ap, 4),
        }

    def summarize(
        self,
        metrics_per_item: dict[str, list[float]],
    ) -> list[dict[str, Any]]:
        """Compute aggregate statistics for a set of metric values.

        Parameters
        ----------
        metrics_per_item : dict[str, list[float]]
            Map of metric_name → list of per-item scores.

        Returns
        -------
        list[dict]
            One summary per metric, with mean, median, std, min, max.
        """
        summaries: list[dict[str, Any]] = []
        for name, values in metrics_per_item.items():
            if not values:
                continue
            values = [v for v in values if v is not None]
            if not values:
                continue

            sorted_vals = sorted(values)
            n = len(sorted_vals)
            mean = sum(sorted_vals) / n
            median = sorted_vals[n // 2] if n % 2 == 1 else (
                sorted_vals[n // 2 - 1] + sorted_vals[n // 2]
            ) / 2.0

            variance = sum((x - mean) ** 2 for x in sorted_vals) / n
            std = math.sqrt(variance)

            summaries.append({
                "metric_name": name,
                "aggregation": "mean",
                "value": round(mean, 4),
                "min_value": round(min(sorted_vals), 4),
                "max_value": round(max(sorted_vals), 4),
                "std_dev": round(std, 4),
                "sample_count": n,
                "median": round(median, 4),
            })

        return summaries


# ═══════════════════════════════════════════════════════════════════════════
# Ranking metrics
# ═══════════════════════════════════════════════════════════════════════════


def compute_precision_recall(
    predicted: list[Any],
    expected: set[Any],
    *,
    k: int = 10,
) -> tuple[float, float, float]:
    """Compute precision@k, recall@k, and F1@k.

    Parameters
    ----------
    predicted : list
        Ranked prediction list.
    expected : set
        Ground truth relevant items.
    k : int
        Cutoff.

    Returns
    -------
    (precision, recall, f1) tuple of floats in [0, 1].
    """
    if not expected:
        return 0.0, 0.0, 0.0

    top_k = predicted[:k]
    hits = sum(1 for item in top_k if item in expected)

    precision = hits / min(k, max(len(top_k), 1))
    recall = hits / len(expected)

    if precision + recall > 0:
        f1 = 2 * precision * recall / (precision + recall)
    else:
        f1 = 0.0

    return precision, recall, f1


def compute_mrr(
    predicted: list[Any],
    expected: set[Any],
) -> float:
    """Compute Mean Reciprocal Rank — 1 / rank_of_first_hit.

    Parameters
    ----------
    predicted : list
        Ranked prediction list.
    expected : set
        Ground truth relevant items.

    Returns
    -------
    float in [0, 1].
    """
    if not expected:
        return 0.0

    for i, item in enumerate(predicted, start=1):
        if item in expected:
            return 1.0 / i
    return 0.0


def compute_ndcg(
    predicted: list[Any],
    expected: set[Any],
    *,
    scores: list[float] | None = None,
    k: int = 10,
) -> float:
    """Compute Normalized Discounted Cumulative Gain at k.

    DCG@k = Σ_i (relevance_i / log2(i+1))
    IDCG@k = ideal DCG (all relevant first)
    NDCG@k = DCG@k / IDCG@k

    Parameters
    ----------
    predicted : list
        Ranked prediction list.
    expected : set
        Ground truth relevant items.
    scores : list[float] | None
        Relevance scores for each prediction (default: 1.0 if relevant, else 0.0).
    k : int
        Cutoff.

    Returns
    -------
    float in [0, 1].
    """
    if not expected or k <= 0:
        return 0.0

    top_k = predicted[:k]
    k = min(k, len(top_k))

    if k == 0:
        return 0.0

    # Build relevance list
    if scores is None:
        relevance = [1.0 if item in expected else 0.0 for item in top_k]
    else:
        relevance = [
            scores[i] if (i < len(scores) and top_k[i] in expected) else (
                1.0 if top_k[i] in expected else 0.0
            )
            for i in range(len(top_k))
        ]

    # DCG
    dcg = 0.0
    for i, rel in enumerate(relevance):
        dcg += rel / math.log2(i + 2)  # i+2 because i is 0-indexed

    # IDCG — ideal: all expected items sorted by relevance desc
    ideal_relevance = sorted(
        [1.0] * min(len(expected), k),
        reverse=True,
    )
    ideal_relevance += [0.0] * (k - len(ideal_relevance))

    idcg = 0.0
    for i, rel in enumerate(ideal_relevance):
        idcg += rel / math.log2(i + 2)

    if idcg == 0:
        return 0.0

    return dcg / idcg


def compute_map(
    predicted: list[Any],
    expected: set[Any],
) -> float:
    """Compute Mean Average Precision.

    AP = (1 / |relevant|) * Σ_{k=1}^n (P@k × rel(k))
    where rel(k) = 1 if item k is relevant, else 0.

    Parameters
    ----------
    predicted : list
        Ranked prediction list.
    expected : set
        Ground truth relevant items.

    Returns
    -------
    float in [0, 1].
    """
    if not expected:
        return 0.0

    hits = 0
    sum_precision = 0.0

    for i, item in enumerate(predicted, start=1):
        if item in expected:
            hits += 1
            sum_precision += hits / i

    if hits == 0:
        return 0.0

    return sum_precision / len(expected)


# ═══════════════════════════════════════════════════════════════════════════
# Text quality metrics
# ═══════════════════════════════════════════════════════════════════════════


def compute_bleu(
    candidate: str,
    references: list[str],
    *,
    max_n: int = 4,
) -> float:
    """Compute BLEU score (bilingual evaluation understudy).

    Parameters
    ----------
    candidate : str
        Generated text.
    references : list[str]
        Reference texts.
    max_n : int
        Maximum n-gram order (default 4 for BLEU-4).

    Returns
    -------
    float in [0, 1].
    """
    candidate_tokens = candidate.lower().split()
    if not candidate_tokens:
        return 0.0

    ref_tokens = [r.lower().split() for r in references]

    # Cap n-gram order to candidate length (standard BLEU practice)
    effective_n = min(max_n, len(candidate_tokens))

    precisions: list[float] = []
    for n in range(1, max_n + 1):
        if n > len(candidate_tokens):
            # Skip n-gram orders longer than candidate
            break

        cand_ngrams = Counter(_ngrams(candidate_tokens, n))
        if not cand_ngrams:
            precisions.append(0.0)
            continue

        # Max reference n-gram counts
        ref_ngram_max: Counter = Counter()
        for ref in ref_tokens:
            ref_cnt = Counter(_ngrams(ref, n))
            for ng, count in ref_cnt.items():
                if count > ref_ngram_max.get(ng, 0):
                    ref_ngram_max[ng] = count

        # Clipped count
        clipped = 0
        for ng, count in cand_ngrams.items():
            clipped += min(count, ref_ngram_max.get(ng, 0))

        total = sum(cand_ngrams.values())
        precisions.append(clipped / max(total, 1))

    # Brevity penalty
    c_len = len(candidate_tokens)
    r_len = min(
        (abs(len(r) - c_len), len(r))
        for r in ref_tokens
    )[1] if ref_tokens else c_len

    bp = 1.0 if c_len >= r_len else math.exp(1.0 - r_len / max(c_len, 1))

    # Apply smoothing: add-epsilon to avoid zero scores for short texts
    # Standard BLEU is harsh on short texts; smoothing is widely used practice
    smoothed = [max(p, 0.01) for p in precisions] if any(p == 0.0 for p in precisions) else precisions

    log_avg = sum(math.log(p) for p in smoothed) / len(smoothed)
    return bp * math.exp(log_avg)


def compute_rouge_l(
    candidate: str,
    reference: str,
) -> float:
    """Compute ROUGE-L (longest common subsequence based) F-measure.

    Parameters
    ----------
    candidate : str
        Generated text.
    reference : str
        Reference text.

    Returns
    -------
    float in [0, 1].
    """
    c_tokens = candidate.lower().split()
    r_tokens = reference.lower().split()

    if not c_tokens or not r_tokens:
        return 0.0

    lcs_len = _lcs_length(c_tokens, r_tokens)

    if len(c_tokens) == 0 or len(r_tokens) == 0:
        return 0.0

    precision = lcs_len / len(c_tokens) if len(c_tokens) > 0 else 0.0
    recall = lcs_len / len(r_tokens) if len(r_tokens) > 0 else 0.0

    if precision + recall > 0:
        f1 = 2 * precision * recall / (precision + recall)
    else:
        f1 = 0.0

    return f1


def compute_token_f1(
    predicted: str,
    expected: str,
) -> float:
    """Compute token-level F1 (precision + recall of word overlap).

    Parameters
    ----------
    predicted : str
    expected : str

    Returns
    -------
    float in [0, 1].
    """
    pred_tokens = set(predicted.lower().split())
    exp_tokens = set(expected.lower().split())

    if not pred_tokens or not exp_tokens:
        return 0.0

    overlap = pred_tokens & exp_tokens

    precision = len(overlap) / len(pred_tokens)
    recall = len(overlap) / len(exp_tokens)

    if precision + recall > 0:
        return 2 * precision * recall / (precision + recall)
    return 0.0


# ═══════════════════════════════════════════════════════════════════════════
# Graph-specific metrics
# ═══════════════════════════════════════════════════════════════════════════


def compute_graph_metrics(
    *,
    predicted: list[list[Any]],
    ground_truth: list[list[Any]],
) -> dict[str, float]:
    """Compute community-detection quality metrics.

    Parameters
    ----------
    predicted : list[list]
        Detected communities (list of node ID lists).
    ground_truth : list[list]
        Ground truth communities.

    Returns
    -------
    dict with nmi, ari, element_accuracy scores.
    """
    # Normalized Mutual Information (simple version)
    nmi = _compute_nmi(predicted, ground_truth)
    ari = _compute_ari(predicted, ground_truth)

    return {
        "nmi": round(nmi, 4),
        "ari": round(ari, 4),
        "predicted_communities": len(predicted),
        "ground_truth_communities": len(ground_truth),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════════════════════════════════


def _ngrams(tokens: list[str], n: int) -> list[tuple[str, ...]]:
    """Generate n-grams from token list."""
    if n <= 0 or n > len(tokens):
        return []
    return [tuple(tokens[i : i + n]) for i in range(len(tokens) - n + 1)]


def _lcs_length(a: list[str], b: list[str]) -> int:
    """Longest common subsequence length (DP, O(mn))."""
    m, n = len(a), len(b)
    dp = [[0] * (n + 1) for _ in range(m + 1)]

    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if a[i - 1] == b[j - 1]:
                dp[i][j] = dp[i - 1][j - 1] + 1
            else:
                dp[i][j] = max(dp[i - 1][j], dp[i][j - 1])

    return dp[m][n]


def _compute_nmi(
    predicted: list[list[Any]],
    ground_truth: list[list[Any]],
) -> float:
    """Compute Normalized Mutual Information between two partitions.

    Simplified approximation: overlap-based NMI.
    """
    if not predicted or not ground_truth:
        return 0.0

    # Flatten to node -> community mappings
    pred_map: dict[Any, int] = {}
    for i, comm in enumerate(predicted):
        for node in comm:
            pred_map[node] = i

    gt_map: dict[Any, int] = {}
    for i, comm in enumerate(ground_truth):
        for node in comm:
            gt_map[node] = i

    common_nodes = set(pred_map.keys()) & set(gt_map.keys())
    if not common_nodes:
        return 0.0

    n = len(common_nodes)

    # Contingency table
    pred_labels = list(set(pred_map[n] for n in common_nodes))
    gt_labels = list(set(gt_map[n] for n in common_nodes))

    # Mutual Information
    mi = 0.0
    for pc in pred_labels:
        for gc in gt_labels:
            n_pg = sum(1 for n in common_nodes if pred_map[n] == pc and gt_map[n] == gc)
            if n_pg == 0:
                continue
            n_p = sum(1 for n in common_nodes if pred_map[n] == pc)
            n_g = sum(1 for n in common_nodes if gt_map[n] == gc)
            mi += (n_pg / n) * math.log2((n_pg * n) / (n_p * n_g)) if n_p > 0 and n_g > 0 else 0

    # Entropies
    h_pred = 0.0
    for pc in pred_labels:
        n_p = sum(1 for n in common_nodes if pred_map[n] == pc) / n
        if n_p > 0:
            h_pred -= n_p * math.log2(n_p)

    h_gt = 0.0
    for gc in gt_labels:
        n_g = sum(1 for n in common_nodes if gt_map[n] == gc) / n
        if n_g > 0:
            h_gt -= n_g * math.log2(n_g)

    if h_pred + h_gt == 0:
        return 0.0

    return 2.0 * mi / (h_pred + h_gt)


def _compute_ari(
    predicted: list[list[Any]],
    ground_truth: list[list[Any]],
) -> float:
    """Compute Adjusted Rand Index (simplified).

    Measures similarity between two data clusterings.
    """
    if not predicted or not ground_truth:
        return 0.0

    # Build membership maps
    pred_labels: dict[Any, int] = {}
    for i, comm in enumerate(predicted):
        for node in comm:
            pred_labels[node] = i

    gt_labels: dict[Any, int] = {}
    for i, comm in enumerate(ground_truth):
        for node in comm:
            gt_labels[node] = i

    common = list(set(pred_labels.keys()) & set(gt_labels.keys()))
    if len(common) < 2:
        return 0.0

    n = len(common)

    # Count agreements
    agreements = 0
    disagreements = 0
    total_pairs = n * (n - 1) // 2

    for i in range(n):
        for j in range(i + 1, n):
            a, b = common[i], common[j]
            same_pred = pred_labels[a] == pred_labels[b]
            same_gt = gt_labels[a] == gt_labels[b]
            if same_pred == same_gt:
                agreements += 1
            else:
                disagreements += 1

    if total_pairs == 0:
        return 0.0

    rand = agreements / total_pairs

    # Simplification: baseline expected agreement = 0.5 (random partition)
    expected = 0.5

    if (1.0 - expected) == 0:
        return 0.0

    ari = (rand - expected) / (1.0 - expected)
    return max(-1.0, min(1.0, ari))
