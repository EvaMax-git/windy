"""P6-02 Memory Refine — automated memory lifecycle management.

This package provides five core refinement operations that run on top of the
existing memory, memory_versions, memory_relations, and memory_index_entries
tables.

Modules
-------
* ``dedup.py``   — detect duplicate memories via embedding cosine similarity
* ``conflict.py`` — detect semantic conflicts between memories via LLM
* ``merge.py``    — merge multiple source memories into one unified memory
* ``expire.py``   — sweep and expire stale / low-quality memories
* ``quality.py``  — compute quality scores and attach search weight penalties

All write operations go through ``write_with_audit_outbox_idempotency``.
"""

from __future__ import annotations

from mneme.memory.refine.dedup import (
    DedupPair,
    DedupResult,
    apply_dedup,
    apply_dedup_batch,
    detect_duplicates,
)
from mneme.memory.refine.conflict import (
    ConflictCandidate,
    ConflictResult,
    apply_conflict,
    apply_conflict_batch,
    detect_conflicts,
    evaluate_conflicts_with_llm,
    run_conflict_pipeline,
)
from mneme.memory.refine.merge import (
    MergeResult,
    SmartMergeOutput,
    quick_merge,
    smart_merge,
)
from mneme.memory.refine.expire import (
    DEFAULT_RULES as EXPIRE_DEFAULT_RULES,
    ExpireApplyOutput,
    ExpireCandidate,
    ExpireRule,
    ExpireScanOutput,
    apply_expire,
    apply_expire_batch,
    scan_expire_candidates,
)
from mneme.memory.refine.quality import (
    QualityBatchOutput,
    QualityResult,
    apply_quality_scores,
    score_memories,
)

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import logging

logger = logging.getLogger(__name__)


# ── Pipeline orchestration ──────────────────────────────────────────────────────


@dataclass
class RefinePipelineResult:
    """Aggregated result of ``run_refine_pipeline()``."""

    dedup: DedupResult | None = None
    conflict: ConflictResult | None = None
    expire: ExpireScanOutput | None = None
    expire_applied: ExpireApplyOutput | None = None
    quality: QualityBatchOutput | None = None
    errors: list[str] = field(default_factory=list)
    stages_run: list[str] = field(default_factory=list)


def run_refine_pipeline(
    db,
    context,
    *,
    project_id: UUID | None = None,
    gateway=None,
    stages: list[str] | None = None,
    dry_run: bool = False,
    dedup_threshold: float = 0.92,
    conflict_threshold_low: float = 0.70,
    conflict_threshold_high: float = 0.92,
    min_confidence: float = 0.7,
    max_candidates: int = 50,
    create_review: bool = True,
) -> RefinePipelineResult:
    """Run the full memory refinement pipeline.

    Executes the following stages in order (configurable via *stages*):

    1. **dedup** — detect near-duplicate memories via embedding cosine similarity.
    2. **conflict** — detect semantically conflicting memories via LLM.
    3. **expire** — sweep stale / low-quality memories for expiration.
    4. **quality** — compute quality scores and search weights.

    Parameters
    ----------
    db : Session
        Active SQLAlchemy session.
    context : RequestContext
        Tracing / auth context for audit + outbox.
    project_id : UUID | None
        Scope to one project.  ``None`` = all projects.
    gateway : Gateway | None
        Gateway instance for LLM calls (needed for conflict + quality coherence).
    stages : list[str] | None
        Subset of stages to run.  ``None`` runs all four.
    dry_run : bool
        If ``True``, detection stages run but no DB writes are made for
        dedup / conflict.  Expire and quality always write (they are
        idempotent).
    dedup_threshold : float
        Cosine similarity threshold for dedup detection.
    conflict_threshold_low / conflict_threshold_high : float
        Similarity range for conflict zone.
    min_confidence : float
        Minimum LLM confidence to create conflict relations.
    max_candidates : int
        Max candidates per detection stage.
    create_review : bool
        Whether to create review items for dedup / conflict detections.

    Returns
    -------
    RefinePipelineResult
    """
    allowed_stages = {"dedup", "conflict", "expire", "quality"}
    effective_stages = stages if stages else sorted(allowed_stages)

    # Validate stage names
    for s in effective_stages:
        if s not in allowed_stages:
            raise ValueError(f"Unknown refine stage: {s!r}. Allowed: {allowed_stages}")

    result = RefinePipelineResult()

    # ── Stage 1: Dedup ──────────────────────────────────────────────────────
    if "dedup" in effective_stages:
        try:
            logger.info("refine-pipeline: stage=dedup (dry_run=%s)", dry_run)
            dedup_result = detect_duplicates(
                db,
                project_id=project_id,
                threshold=dedup_threshold,
                max_candidates=max_candidates,
            )
            if not dry_run and dedup_result.pairs:
                dedup_result = apply_dedup_batch(
                    db,
                    context,
                    pairs=dedup_result.pairs,
                    create_review=create_review,
                )
            result.dedup = dedup_result
            result.stages_run.append("dedup")
        except Exception as exc:
            msg = f"dedup stage failed: {exc}"
            logger.exception(msg)
            result.errors.append(msg)

    # ── Stage 2: Conflict ───────────────────────────────────────────────────
    if "conflict" in effective_stages:
        try:
            logger.info("refine-pipeline: stage=conflict (dry_run=%s)", dry_run)
            conflict_result = detect_conflicts(
                db,
                project_id=project_id,
                threshold_low=conflict_threshold_low,
                threshold_high=conflict_threshold_high,
                max_pairs=max_candidates,
            )
            if gateway is not None and conflict_result.candidates:
                conflict_result = evaluate_conflicts_with_llm(
                    conflict_result.candidates,
                    gateway=gateway,
                    project_id=project_id,
                )
            if not dry_run and conflict_result.conflicts_confirmed > 0:
                conflict_result = apply_conflict_batch(
                    db,
                    context,
                    candidates=conflict_result.candidates,
                    min_confidence=min_confidence,
                    create_review=create_review,
                )
            result.conflict = conflict_result
            result.stages_run.append("conflict")
        except Exception as exc:
            msg = f"conflict stage failed: {exc}"
            logger.exception(msg)
            result.errors.append(msg)

    # ── Stage 3: Expire ─────────────────────────────────────────────────────
    if "expire" in effective_stages:
        try:
            logger.info("refine-pipeline: stage=expire")
            expire_scan = scan_expire_candidates(
                db,
                project_id=project_id,
                max_candidates=max_candidates,
            )
            result.expire = expire_scan
            if not dry_run and expire_scan.candidates:
                expire_applied = apply_expire_batch(
                    db,
                    context,
                    candidates=expire_scan.candidates,
                )
                result.expire_applied = expire_applied
            result.stages_run.append("expire")
        except Exception as exc:
            msg = f"expire stage failed: {exc}"
            logger.exception(msg)
            result.errors.append(msg)

    # ── Stage 4: Quality ────────────────────────────────────────────────────
    if "quality" in effective_stages:
        try:
            logger.info("refine-pipeline: stage=quality")
            quality_result = score_memories(
                db,
                project_id=project_id,
                context=context,
            )
            result.quality = quality_result
            result.stages_run.append("quality")
        except Exception as exc:
            msg = f"quality stage failed: {exc}"
            logger.exception(msg)
            result.errors.append(msg)

    logger.info(
        "refine-pipeline: complete — stages=%s, errors=%d",
        result.stages_run,
        len(result.errors),
    )
    return result


__all__ = [
    # pipeline
    "RefinePipelineResult",
    "run_refine_pipeline",
    # dedup
    "DedupPair",
    "DedupResult",
    "detect_duplicates",
    "apply_dedup",
    "apply_dedup_batch",
    # conflict
    "ConflictCandidate",
    "ConflictResult",
    "detect_conflicts",
    "evaluate_conflicts_with_llm",
    "apply_conflict",
    "apply_conflict_batch",
    "run_conflict_pipeline",
    # merge
    "MergeResult",
    "SmartMergeOutput",
    "smart_merge",
    "quick_merge",
    # expire
    "EXPIRE_DEFAULT_RULES",
    "ExpireApplyOutput",
    "ExpireCandidate",
    "ExpireRule",
    "ExpireScanOutput",
    "apply_expire",
    "apply_expire_batch",
    "scan_expire_candidates",
    # quality
    "QualityBatchOutput",
    "QualityResult",
    "score_memories",
    "apply_quality_scores",
]
