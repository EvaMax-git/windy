"""P6-02 Memory Refine API — expose refine engine operations as endpoints.

Endpoints
---------
* ``POST   /api/v4/refine/dedup``      — detect & create duplicate memory relations
* ``POST   /api/v4/refine/conflict``    — detect & evaluate conflicts via LLM
* ``POST   /api/v4/refine/merge``       — LLM-assisted smart merge of memories
* ``POST   /api/v4/refine/expire/scan`` — scan for expiration candidates
* ``POST   /api/v4/refine/expire/apply``— apply expiration to candidates
* ``POST   /api/v4/refine/quality``     — compute quality scores & search weights
* ``POST   /api/v4/refine/pipeline``    — run full refine pipeline
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from mneme.api.context import RequestContext, get_request_context
from mneme.api.errors import ApiError
from mneme.api.schemas import envelope
from mneme.db.base import get_db
from mneme.memory.refine import (
    apply_dedup_batch,
    apply_expire_batch,
    detect_conflicts,
    detect_duplicates,
    evaluate_conflicts_with_llm,
    apply_conflict_batch,
    run_refine_pipeline,
    scan_expire_candidates,
    score_memories,
    smart_merge,
)
from mneme.schemas.refine import (
    ConflictCandidate as ConflictCandidateSchema,
    DedupCandidate,
    ExpireCandidate as ExpireCandidateSchema,
    MergeRequest,
    QualityResult as QualityResultSchema,
    RefineRunRequest,
    RefineRunResponse,
)

router = APIRouter(prefix="/refine", tags=["refine"])


# ──────────────────────────────────────────────────────────────────────────────
# POST /refine/dedup
# ──────────────────────────────────────────────────────────────────────────────


@router.post("/dedup", response_model=dict)
def dedup_endpoint(
    project_id: UUID | None = None,
    threshold: float = 0.92,
    max_candidates: int = 50,
    dry_run: bool = False,
    create_review: bool = True,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Detect near-duplicate memories via embedding cosine similarity.

    Query parameters
    ----------------
    * ``project_id`` — scope to a single project (optional).
    * ``threshold`` — cosine-similarity threshold (0.5-1.0, default 0.92).
    * ``max_candidates`` — max pairs to return (1-500, default 50).
    * ``dry_run`` — if True, detection only; no DB writes.
    * ``create_review`` — whether to create review items for human confirmation.

    When ``dry_run=False`` and duplicates are found, the endpoint:
    1. Creates ``memory_relations(type='duplicates')`` rows.
    2. Optionally creates ``review_items`` for human confirmation.
    """
    try:
        result = detect_duplicates(
            db,
            project_id=project_id,
            threshold=threshold,
            max_candidates=max_candidates,
        )

        pairs_applied = 0
        dedup_pairs = []

        if not dry_run and result.pairs:
            applied = apply_dedup_batch(
                db,
                context,
                pairs=result.pairs,
                create_review=create_review,
            )
            pairs_applied = applied.relations_created
            dedup_pairs = [
                DedupCandidate(
                    memory_a_id=p.memory_a_id,
                    memory_b_id=p.memory_b_id,
                    similarity=p.similarity,
                    memory_a_key=p.canonical_key_a,
                    memory_b_key=p.canonical_key_b,
                    memory_a_title=p.memory_a_title,
                    memory_b_title=p.memory_b_title,
                )
                for p in applied.pairs
            ]
        elif result.pairs:
            dedup_pairs = [
                DedupCandidate(
                    memory_a_id=p.memory_a_id,
                    memory_b_id=p.memory_b_id,
                    similarity=p.similarity,
                    memory_a_key=p.canonical_key_a,
                    memory_b_key=p.canonical_key_b,
                    memory_a_title=p.memory_a_title,
                    memory_b_title=p.memory_b_title,
                )
                for p in result.pairs
            ]

        return envelope(
            {
                "pairs_found": result.pairs_found,
                "relations_created": pairs_applied,
                "dry_run": dry_run,
                "pairs": [p.model_dump(mode="json") for p in dedup_pairs],
            },
            request_id=context.request_id,
            correlation_id=context.correlation_id,
        )
    except ValueError as e:
        raise ApiError(400, "bad_request", str(e))


# ──────────────────────────────────────────────────────────────────────────────
# POST /refine/conflict
# ──────────────────────────────────────────────────────────────────────────────


@router.post("/conflict", response_model=dict)
def conflict_endpoint(
    project_id: UUID | None = None,
    threshold_low: float = 0.70,
    threshold_high: float = 0.92,
    max_pairs: int = 30,
    min_confidence: float = 0.7,
    dry_run: bool = False,
    create_review: bool = True,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Detect semantic conflicts between memory pairs via LLM evaluation.

    Two-stage pipeline:
    1. Similarity zone filter (threshold_low <= sim < threshold_high).
    2. LLM evaluation to determine actual conflicts.

    Query parameters
    ----------------
    * ``project_id`` — scope to a single project (optional).
    * ``threshold_low`` — lower similarity bound (0.0-1.0, default 0.70).
    * ``threshold_high`` — upper similarity bound (0.0-1.0, default 0.92).
    * ``max_pairs`` — max candidate pairs (1-500, default 30).
    * ``min_confidence`` — minimum LLM confidence to create relations (0.0-1.0).
    * ``dry_run`` — if True, detection + LLM evaluation only; no DB writes.
    * ``create_review`` — whether to create review items for human confirmation.

    Requires Gateway LLM access for conflict evaluation.
    """
    try:
        # Stage 1: similarity zone candidates
        result = detect_conflicts(
            db,
            project_id=project_id,
            threshold_low=threshold_low,
            threshold_high=threshold_high,
            max_pairs=max_pairs,
        )

        # Stage 2: LLM evaluation (best-effort if Gateway available)
        candidates_schema = []
        if result.candidates:
            try:
                from mneme.gateway.call import Gateway
                gw = Gateway()
                result = evaluate_conflicts_with_llm(
                    result.candidates,
                    gateway=gw,
                    project_id=project_id,
                )
            except Exception:
                # Gateway unavailable — return candidates without LLM evaluation
                pass

        relations_created = 0
        if not dry_run and result.conflicts_confirmed > 0:
            applied = apply_conflict_batch(
                db,
                context,
                candidates=result.candidates,
                min_confidence=min_confidence,
                create_review=create_review,
            )
            relations_created = applied.relations_created

        for c in result.candidates:
            candidates_schema.append(
                ConflictCandidateSchema(
                    memory_a_id=c.memory_a_id,
                    memory_b_id=c.memory_b_id,
                    similarity=c.similarity,
                    conflict=c.conflict,
                    reason=c.reason,
                    confidence=c.confidence,
                    memory_a_key=c.canonical_key_a,
                    memory_b_key=c.canonical_key_b,
                )
            )

        return envelope(
            {
                "candidates_found": result.candidates_found,
                "llm_evaluated": result.llm_evaluated,
                "conflicts_confirmed": result.conflicts_confirmed,
                "relations_created": relations_created,
                "dry_run": dry_run,
                "candidates": [c.model_dump(mode="json") for c in candidates_schema],
            },
            request_id=context.request_id,
            correlation_id=context.correlation_id,
        )
    except ValueError as e:
        raise ApiError(400, "bad_request", str(e))


# ──────────────────────────────────────────────────────────────────────────────
# POST /refine/merge
# ──────────────────────────────────────────────────────────────────────────────


@router.post("/merge", response_model=dict, status_code=200)
def merge_endpoint(
    body: MergeRequest,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """LLM-assisted smart merge of multiple consumed memories into a survivor.

    Request body
    ------------
    * ``survivor_memory_id`` — the memory that absorbs the others.
    * ``consumed_memory_ids`` — list of memory IDs to merge into survivor.
    * ``reason`` — optional human-readable reason for the merge.

    The endpoint:
    1. Loads all involved memories.
    2. Calls the LLM to produce a merged, coherent text.
    3. Updates the survivor's content.
    4. Calls ``merge_memory()`` for each consumed memory.
    5. Returns the updated survivor and per-consumed results.
    """
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "Idempotency-Key header required")

    try:
        output = smart_merge(
            db,
            context,
            survivor_id=body.survivor_memory_id,
            consumed_ids=body.consumed_memory_ids,
            reason=body.reason,
            use_llm=True,
        )
    except ValueError as e:
        raise ApiError(400, "bad_request", str(e))

    survivor_data = None
    if output.survivor:
        survivor_data = output.survivor.model_dump(mode="json")

    results_data = []
    for r in output.results:
        results_data.append({
            "survivor_id": str(r.survivor_id),
            "consumed_id": str(r.consumed_id),
            "success": r.success,
            "error": r.error,
        })

    return envelope(
        {
            "merged_count": output.merged_count,
            "failed_count": output.failed_count,
            "merged_title": output.merged_title,
            "merged_text": output.merged_text,
            "survivor": survivor_data,
            "results": results_data,
        },
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ──────────────────────────────────────────────────────────────────────────────
# POST /refine/expire/scan
# ──────────────────────────────────────────────────────────────────────────────


@router.post("/expire/scan", response_model=dict)
def expire_scan_endpoint(
    project_id: UUID | None = None,
    max_candidates: int = 50,
    min_quality: float = 0.3,
    max_age_days: int = 30,
    min_weight: float = 0.2,
    stale_days: int = 90,
    min_conflicts: int = 3,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Scan for memory expiration candidates using configured rules.

    Query parameters
    ----------------
    * ``project_id`` — scope to a single project (optional).
    * ``max_candidates`` — max candidates per rule (1-500, default 50).
    * ``min_quality`` — quality threshold for ``low_quality_old`` rule.
    * ``max_age_days`` — minimum age for ``low_quality_old`` rule.
    * ``min_weight`` — search weight threshold for ``zero_weight_stale`` rule.
    * ``stale_days`` — days without updates for ``zero_weight_stale`` rule.
    * ``min_conflicts`` — minimum conflict count for ``high_conflict_count`` rule.

    Rules applied:
    1. ``low_quality_old``   — quality_score < min_quality AND age > max_age_days
    2. ``zero_weight_stale`` — search_weight < min_weight AND no updates in stale_days
    3. ``high_conflict_count`` — 3+ conflicts_with relations
    4. ``merged_consumed``   — status='merged'
    """
    try:
        output = scan_expire_candidates(
            db,
            project_id=project_id,
            max_candidates=max_candidates,
            min_quality=min_quality,
            max_age_days=max_age_days,
            min_weight=min_weight,
            stale_days=stale_days,
            min_conflicts=min_conflicts,
        )

        candidates = []
        for c in output.candidates:
            created_at_val = None
            if hasattr(c, "created_at") and c.created_at is not None:
                if hasattr(c.created_at, "isoformat"):
                    created_at_val = c.created_at.isoformat()
                else:
                    created_at_val = str(c.created_at)
            candidates.append(
                ExpireCandidateSchema(
                    memory_id=c.memory_id,
                    canonical_key=c.canonical_key,
                    reason=c.reason,
                    quality_score=c.quality_score,
                    search_weight=c.search_weight,
                    created_at=created_at_val,
                )
            )

        return envelope(
            {
                "total_scanned": output.total_scanned,
                "candidates_count": len(output.candidates),
                "rules_used": output.rules_used,
                "candidates": [c.model_dump(mode="json") for c in candidates],
            },
            request_id=context.request_id,
            correlation_id=context.correlation_id,
        )
    except ValueError as e:
        raise ApiError(400, "bad_request", str(e))


# ──────────────────────────────────────────────────────────────────────────────
# POST /refine/expire/apply
# ──────────────────────────────────────────────────────────────────────────────


@router.post("/expire/apply", response_model=dict, status_code=200)
def expire_apply_endpoint(
    project_id: UUID | None = None,
    max_candidates: int = 50,
    min_quality: float = 0.3,
    max_age_days: int = 30,
    min_weight: float = 0.2,
    stale_days: int = 90,
    min_conflicts: int = 3,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Scan AND apply expiration to candidates in one call.

    First scans for expiration candidates using the configured rules, then
    applies expiration to all found candidates.  Use ``/refine/expire/scan``
    if you want to preview candidates first.

    Query parameters are the same as ``POST /refine/expire/scan``.
    """
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "Idempotency-Key header required")

    try:
        scan = scan_expire_candidates(
            db,
            project_id=project_id,
            max_candidates=max_candidates,
            min_quality=min_quality,
            max_age_days=max_age_days,
            min_weight=min_weight,
            stale_days=stale_days,
            min_conflicts=min_conflicts,
        )

        applied = None
        if scan.candidates:
            applied = apply_expire_batch(
                db,
                context,
                candidates=scan.candidates,
            )

        return envelope(
            {
                "scanned": scan.total_scanned,
                "candidates_found": len(scan.candidates),
                "expired_count": applied.expired_count if applied else 0,
                "failed_count": applied.failed_count if applied else 0,
                "errors": applied.errors if applied else [],
                "rules_used": scan.rules_used,
            },
            request_id=context.request_id,
            correlation_id=context.correlation_id,
        )
    except ValueError as e:
        raise ApiError(400, "bad_request", str(e))


# ──────────────────────────────────────────────────────────────────────────────
# POST /refine/quality
# ──────────────────────────────────────────────────────────────────────────────


@router.post("/quality", response_model=dict)
def quality_endpoint(
    project_id: UUID | None = None,
    max_memories: int = 200,
    batch_size: int = 10,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Compute quality scores and search weights for memories.

    Scoring dimensions:
    * confidence (30%) — source/candidate confidence score
    * evidence  (20%) — number of evidence spans / sources
    * coherence (25%) — LLM-evaluated text quality
    * recency   (15%) — time-decay factor
    * relation  (10%) — supports minus conflicts

    Results are written back to ``memories.quality_score`` and
    ``memories.search_weight``, as well as ``memory_index_entries.search_weight``.

    Query parameters
    ----------------
    * ``project_id`` — scope to a single project (optional).
    * ``max_memories`` — max memories to score (default 200).
    * ``batch_size`` — batch size for LLM coherence evaluation (default 10).
    """
    try:
        output = score_memories(
            db,
            project_id=project_id,
            context=context,
            max_memories=max_memories,
            batch_size=batch_size,
        )

        results_schema = []
        for r in output.results:
            results_schema.append(
                QualityResultSchema(
                    memory_id=r.memory_id,
                    quality_score=r.quality_score,
                    search_weight=r.search_weight,
                    confidence_component=r.confidence_score,
                    evidence_component=r.evidence_count_score,
                    coherence_component=r.text_coherence_score,
                    recency_component=r.recency_score,
                    relation_component=r.relation_score,
                )
            )

        return envelope(
            {
                "total_scored": output.total_scored,
                "total_failed": output.total_failed,
                "overall_stats": output.overall_stats,
                "results": [r.model_dump(mode="json") for r in results_schema],
            },
            request_id=context.request_id,
            correlation_id=context.correlation_id,
        )
    except ValueError as e:
        raise ApiError(400, "bad_request", str(e))


# ──────────────────────────────────────────────────────────────────────────────
# POST /refine/pipeline
# ──────────────────────────────────────────────────────────────────────────────


@router.post("/pipeline", response_model=dict, status_code=200)
def pipeline_endpoint(
    body: RefineRunRequest,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Run the full memory refinement pipeline.

    Executes the specified stages in order: dedup → conflict → expire → quality.

    Request body (``RefineRunRequest``)
    -----------------------------------
    * ``project_id`` — required project scope.
    * ``operations`` — which stages to run (default all four).
    * ``dry_run`` — if True, detection only; no DB writes for dedup/conflict.
      Expire and quality always write (they are idempotent).
    * ``similarity_threshold`` — cosine similarity threshold for dedup (0.5-1.0).
    * ``max_candidates`` — max candidates per detection stage (1-500).
    """
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "Idempotency-Key header required")

    try:
        # Try to get Gateway for conflict evaluation
        gateway = None
        try:
            from mneme.gateway.call import Gateway
            gateway = Gateway()
        except Exception:
            pass

        result = run_refine_pipeline(
            db,
            context,
            project_id=body.project_id,
            gateway=gateway,
            stages=body.operations if body.operations else None,
            dry_run=body.dry_run,
            dedup_threshold=body.similarity_threshold,
            max_candidates=body.max_candidates,
        )

        dedup_pairs = 0
        if result.dedup:
            dedup_pairs = result.dedup.pairs_found

        conflicts_found = 0
        if result.conflict:
            conflicts_found = result.conflict.conflicts_confirmed

        merges_executed = 0  # merge is not auto-executed in pipeline
        expires_executed = 0
        if result.expire_applied:
            expires_executed = result.expire_applied.expired_count

        quality_scored = 0
        if result.quality:
            quality_scored = result.quality.total_scored

        response = RefineRunResponse(
            dedup_pairs_found=dedup_pairs,
            conflicts_found=conflicts_found,
            merges_executed=merges_executed,
            expires_executed=expires_executed,
            quality_scored=quality_scored,
            details=[
                {
                    "stages_run": result.stages_run,
                    "errors": result.errors,
                }
            ],
        )

        return envelope(
            response.model_dump(mode="json"),
            request_id=context.request_id,
            correlation_id=context.correlation_id,
        )
    except ValueError as e:
        raise ApiError(400, "bad_request", str(e))
    except Exception as e:
        raise ApiError(500, "refine_internal_error", str(e))
