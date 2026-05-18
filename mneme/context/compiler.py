"""P5-04 Context Compiler core logic.

Orchestrates context assembly for agent queries:

1. Permission / sensitivity ceiling check
2. Knowledge retrieval (FTS search on knowledge_chunks)
3. Memory retrieval (FTS + ILIKE fallback on memory_index_entries)
4. Rank + sort by score
5. Token budget trimming
6. Write context_packs + context_pack_items
7. Audit events
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from mneme.api.context import RequestContext
from mneme.db.audit import AuditEvent, OutboxEvent, add_audit_event, add_outbox_event
from mneme.db.context_packs import (
    create_context_pack,
    create_context_pack_item,
)
from mneme.db.transactions import transaction
from mneme.knowledge.fts import search_fts as knowledge_fts_search
from mneme.memory.fts import search_fts as memory_fts_search
from mneme.knowledge.token_estimator import estimate_tokens
from mneme.search.reranker import (
    RerankCandidate,
    RerankerError,
    get_reranker,
)

logger = logging.getLogger(__name__)

# Sensitivity ordinals for ceiling checks
_SENSITIVITY_ORDINAL = {
    "public": 0,
    "normal": 1,
    "private": 2,
    "sensitive": 3,
    "secret": 4,
}


def _sensitivity_allowed(item_sensitivity: str, ceiling: str) -> bool:
    """Return True if item_sensitivity <= ceiling."""
    item_ord = _SENSITIVITY_ORDINAL.get(item_sensitivity, 1)
    ceil_ord = _SENSITIVITY_ORDINAL.get(ceiling, 2)
    return item_ord <= ceil_ord


def _content_hash(text: str) -> str:
    """SHA-256 of content for content_digest."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def compile_context(
    db: Session,
    context: RequestContext,
    *,
    agent_id: UUID | None = None,
    project_id: UUID | None = None,
    query_text: str,
    compile_mode: str = "full",
    token_budget: dict[str, Any] | None = None,
    sensitivity_ceiling: str = "private",
) -> dict[str, Any]:
    """Compile a context pack for the given query.

    Parameters
    ----------
    db : Session
        Active SQLAlchemy session.
    context : RequestContext
        Request context with actor info.
    agent_id : UUID | None
        Optional agent requesting the compilation.
    project_id : UUID | None
        Optional project scope.
    query_text : str
        The search query text.
    compile_mode : str
        'full' or 'search_fallback'.
    token_budget : dict
        Token budget configuration.
    sensitivity_ceiling : str
        Maximum sensitivity level to include.

    Returns
    -------
    dict
        A dict with keys: pack (dict), items (list[dict]), total_token_count,
        included_count, excluded_count, degradation_reason.
    """
    budget = token_budget or {}
    max_tokens = budget.get("max_tokens", 4096)
    reserve = budget.get("reserve_for_output", 512)
    knowledge_ratio = budget.get("knowledge_ratio", 0.6)
    memory_ratio = budget.get("memory_ratio", 0.4)

    usable_tokens = max(1, max_tokens - reserve)
    knowledge_budget = int(usable_tokens * knowledge_ratio)
    memory_budget = int(usable_tokens * memory_ratio)

    degradation_reason: str | None = None
    all_items: list[dict[str, Any]] = []
    knowledge_version_set: list[dict[str, Any]] = []
    memory_version_set: list[dict[str, Any]] = []
    exclusion_summary: dict[str, Any] = {"sensitivity_filtered": 0, "budget_trimmed": 0}

    # ── 1. Knowledge retrieval ───────────────────────────────────────────
    knowledge_candidates: list[dict[str, Any]] = []
    try:
        results, total = knowledge_fts_search(
            db,
            query=query_text,
            project_id=project_id,
            sensitivity_floor=None,
            page=1,
            page_size=50,
        )
        for r in results:
            r_dict = r.model_dump() if hasattr(r, "model_dump") else dict(r)
            doc_sens = r_dict.get("document_sensitivity", "normal")
            if not _sensitivity_allowed(doc_sens, sensitivity_ceiling):
                exclusion_summary["sensitivity_filtered"] += 1
                continue
            knowledge_candidates.append({
                "item_type": "knowledge_chunk",
                "object_id": r_dict.get("chunk_id"),
                "source_ref": {
                    "document_id": str(r_dict.get("document_id", "")),
                    "document_title": r_dict.get("document_title", ""),
                    "block_id": str(r_dict.get("block_id", "")) if r_dict.get("block_id") else None,
                    "chunk_text": r_dict.get("chunk_text", "")[:500],
                },
                "score": float(r_dict.get("rank", 0.0)),
                "token_count": estimate_tokens(r_dict.get("chunk_text", "")),
                "reason": "fts_match",
            })
            # Track knowledge version
            knowledge_version_set.append({
                "document_id": str(r_dict.get("document_id", "")),
                "chunk_id": str(r_dict.get("chunk_id", "")),
            })
    except Exception as exc:
        logger.warning("Knowledge FTS search failed: %s", exc)
        if degradation_reason is None:
            degradation_reason = f"knowledge_fts_error: {exc}"

    # ── 2. Memory retrieval ──────────────────────────────────────────────
    memory_candidates: list[dict[str, Any]] = []
    try:
        results, total = memory_fts_search(
            db,
            query=query_text,
            project_id=project_id,
            page=1,
            page_size=50,
        )
        for r in results:
            mem_sens = r.get("sensitivity_level", "normal")
            if not _sensitivity_allowed(mem_sens, sensitivity_ceiling):
                exclusion_summary["sensitivity_filtered"] += 1
                continue
            memory_candidates.append({
                "item_type": "memory",
                "object_id": r.get("memory_id"),
                "source_ref": {
                    "title": r.get("title", ""),
                    "canonical_key": r.get("canonical_key", ""),
                    "memory_text_preview": (r.get("memory_text", "") or "")[:300],
                },
                "score": float(r.get("rank", 0.0)),
                "token_count": estimate_tokens(r.get("memory_text", "")),
                "reason": "fts_match",
            })
            memory_version_set.append({
                "memory_id": str(r.get("memory_id", "")),
                "memory_version": r.get("memory_version"),
                "canonical_key": r.get("canonical_key", ""),
            })
    except Exception as exc:
        logger.warning("Memory FTS search failed: %s", exc)
        if degradation_reason is None:
            degradation_reason = f"memory_fts_error: {exc}"

    # ── 3. Sort by score descending ──────────────────────────────────────
    knowledge_candidates.sort(key=lambda x: x.get("score", 0), reverse=True)
    memory_candidates.sort(key=lambda x: x.get("score", 0), reverse=True)

    # ── 3b. Cross-encoder re-ranking (P5-03) ─────────────────────────────
    # Re-rank the merged candidate pool via cross-encoder for better
    # relevance ordering before token-budget trimming.
    all_candidates = knowledge_candidates + memory_candidates
    if all_candidates and len(all_candidates) > 1:
        rerank_candidates_objs: list[RerankCandidate] = []
        candidate_idx_map: list[int] = []
        for i, item in enumerate(all_candidates):
            source = item.get("source_ref", {})
            if item["item_type"] == "knowledge_chunk":
                text = source.get("chunk_text", "")
            else:
                text = f"{source.get('title', '')}\n{source.get('memory_text_preview', '')}"
            rerank_candidates_objs.append(RerankCandidate(
                id=str(i),
                text=text.strip() or query_text,
                fts_score=item.get("score", 0.0),
                vector_score=0.0,
                meta={"item_type": item["item_type"]},
            ))
            candidate_idx_map.append(i)

        reranker = get_reranker()
        try:
            reranked = reranker.rerank(
                query=query_text,
                candidates=rerank_candidates_objs,
                project_id=project_id,
                context=context,
            )
            score_map: dict[int, float] = {}
            for rr in reranked:
                try:
                    idx = int(rr.id)
                    score_map[idx] = rr.score
                except (ValueError, KeyError):
                    continue
            if score_map:
                for idx, item in enumerate(all_candidates):
                    if idx in score_map:
                        item["score"] = score_map[idx]
                # Re-sort by new scores
                all_candidates.sort(key=lambda x: x.get("score", 0), reverse=True)
                # Re-separate into knowledge / memory groups preserving order
                knowledge_candidates = [c for c in all_candidates if c["item_type"] == "knowledge_chunk"]
                memory_candidates = [c for c in all_candidates if c["item_type"] == "memory"]
                logger.debug("Context compiler reranked %d candidates via cross-encoder", len(score_map))
        except RerankerError as exc:
            logger.warning("Context compiler reranker failed, using original scores: %s", exc)

    # ── 4. Token budget trimming ─────────────────────────────────────────
    included_items: list[dict[str, Any]] = []
    excluded_items: list[dict[str, Any]] = []
    knowledge_token_sum = 0
    memory_token_sum = 0

    for item in knowledge_candidates:
        item_tokens = item.get("token_count", 0) or 0
        if knowledge_token_sum + item_tokens <= knowledge_budget:
            item["included"] = True
            item["exclusion_reason"] = None
            knowledge_token_sum += item_tokens
            included_items.append(item)
        else:
            item["included"] = False
            item["exclusion_reason"] = "token_budget_exceeded"
            excluded_items.append(item)
            exclusion_summary["budget_trimmed"] += 1

    for item in memory_candidates:
        item_tokens = item.get("token_count", 0) or 0
        if memory_token_sum + item_tokens <= memory_budget:
            item["included"] = True
            item["exclusion_reason"] = None
            memory_token_sum += item_tokens
            included_items.append(item)
        else:
            item["included"] = False
            item["exclusion_reason"] = "token_budget_exceeded"
            excluded_items.append(item)
            exclusion_summary["budget_trimmed"] += 1

    all_items = included_items + excluded_items
    total_token_count = knowledge_token_sum + memory_token_sum

    # If no items found and full mode, add fallback_query
    if not all_items and compile_mode == "full":
        all_items.append({
            "item_type": "fallback_query",
            "object_id": None,
            "source_ref": {"query_text": query_text},
            "included": True,
            "exclusion_reason": None,
            "score": 0.0,
            "token_count": estimate_tokens(query_text),
            "reason": "no_results_fallback",
        })
        degradation_reason = degradation_reason or "no_retrieval_results"

    # ── 5. Write context_packs + context_pack_items ──────────────────────
    pack_status = "failed" if compile_mode == "search_fallback" and degradation_reason else "created"

    with transaction(db):
        pack = create_context_pack(
            db,
            context,
            agent_id=agent_id,
            project_id=project_id,
            compile_mode=compile_mode,
            status=pack_status,
            knowledge_version_set=knowledge_version_set,
            memory_version_set=memory_version_set,
            token_budget=budget,
            exclusion_summary=exclusion_summary,
        )

        items_written: list[dict[str, Any]] = []
        for idx, item in enumerate(all_items):
            written = create_context_pack_item(
                db,
                pack_id=pack["context_pack_id"],
                item_order=idx,
                item_type=item.get("item_type", "fallback_query"),
                object_id=item.get("object_id"),
                object_version=item.get("object_version"),
                source_ref=item.get("source_ref", {}),
                included=item.get("included", True),
                exclusion_reason=item.get("exclusion_reason"),
                score=item.get("score"),
                token_count=item.get("token_count"),
                reason=item.get("reason"),
                content_digest=_content_hash(json.dumps(item.get("source_ref", {}), sort_keys=True, default=str)),
            )
            items_written.append(written)

        # ── 6. Audit event ───────────────────────────────────────────────
        add_audit_event(
            db,
            context,
            AuditEvent(
                action="context.compile",
                result="success",
                object_type="context_pack",
                object_id=pack["context_pack_id"],
                project_id=project_id,
                metadata_json={
                    "compile_mode": compile_mode,
                    "query_text": query_text[:200],
                    "total_items": len(all_items),
                    "included_count": len(included_items),
                    "excluded_count": len(excluded_items),
                    "total_token_count": total_token_count,
                    "degradation_reason": degradation_reason,
                },
            ),
        )

        add_outbox_event(
            db,
            context,
            OutboxEvent(
                event_type="context_pack.compiled",
                aggregate_type="context_pack",
                aggregate_id=pack["context_pack_id"],
                aggregate_version=1,
                idempotency_key=str(context.idempotency_key or str(uuid4())),
                payload_json={
                    "compile_mode": compile_mode,
                    "total_items": len(all_items),
                    "total_token_count": total_token_count,
                },
            ),
        )

    return {
        "pack": pack,
        "items": items_written,
        "total_token_count": total_token_count,
        "included_count": len(included_items) if included_items else len([i for i in all_items if i.get("included", True)]),
        "excluded_count": len(excluded_items),
        "degradation_reason": degradation_reason,
    }
