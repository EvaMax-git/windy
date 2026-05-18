"""Global search endpoint — aggregated cross-domain search with cross-encoder reranking.

Endpoints
---------
* ``GET /api/v4/search/global`` — Search across agents, knowledge chunks, and memories.
  Uses PostgreSQL ILIKE + FTS where applicable. Returns unified ranked results.
  Supports LLM-based query rewriting, cross-encoder re-ranking (P5-03),
  PPR graph traversal recall, and temporal shape clustering ("那个夏天" → concept cloud).
"""

from __future__ import annotations

import logging
import time
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.encoders import jsonable_encoder
from sqlalchemy import text
from sqlalchemy.orm import Session

from mneme.api.context import RequestContext, get_request_context
from mneme.api.schemas import envelope
from mneme.config import get_settings
from mneme.db.base import get_db
from mneme.schemas.common import PageInfo, PaginatedData
from mneme.schemas.global_search import GlobalSearchResult, GlobalSearchResponse, QueryRewriteInfo
from mneme.search.reranker import (
    RerankCandidate,
    RerankerError,
    get_reranker,
)

router = APIRouter(prefix="/search/global", tags=["search"])

logger = logging.getLogger(__name__)

_MAX_PER_CATEGORY = 12
_MAX_SNIPPET_LEN = 300
_AGENT_LINK_PREFIX = "/app/agents"
_KNOWLEDGE_LINK_PREFIX = "/app/knowledge"
_MEMORY_LINK_PREFIX = "/app/memory"

# PPR and temporal cluster icons
_PPR_ICON = "git-branch"
_TEMPORAL_ICON = "clock"


def _snippet(text: str | None, query: str, max_len: int = _MAX_SNIPPET_LEN) -> str:
    """Extract a snippet around the first match of *query* in *text*."""
    if not text or not query:
        return (text or "")[:max_len]
    text_lower = text.lower()
    query_lower = query.lower()
    idx = text_lower.find(query_lower)
    if idx == -1:
        return text[:max_len]
    start = max(0, idx - 60)
    end = min(len(text), idx + len(query) + 120)
    result = text[start:end]
    if start > 0:
        result = "…" + result
    if end < len(text):
        result = result + "…"
    return result


# ═══════════════════════════════════════════════════════════════════
# GET /api/v4/search/global
# ═══════════════════════════════════════════════════════════════════

@router.get("", response_model=dict)
def global_search(
    q: str = Query(..., min_length=1, max_length=300, description="Search query"),
    project_id: UUID | None = Query(None, description="Filter by project"),
    rewrite: bool = Query(
        True,
        description="Enable LLM-based query rewriting to resolve vague references "
                    "(e.g. '上次那个bug' → concrete search terms)",
    ),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Results per page"),
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Aggregated global search across agents, knowledge chunks, and memories.

    Searches three domains concurrently with ILIKE-based matching:

    * **Agents** — ``name`` and ``description``
    * **Knowledge** — ``chunk_text`` in ``knowledge_chunks`` joined with ``knowledge_documents``
    * **Memory** — ``title`` and ``memory_text`` in ``memories``

    Each domain returns at most 12 candidates. The union is limited to
    ``page_size`` results per page, ranked by a simple heuristic score.

    Results include a ``source`` discriminator, a display ``title``, a
    context ``snippet``, an ``icon`` hint, and a ``link`` for frontend
    navigation.

    **Query Rewriting**: When ``rewrite=true`` (default), the endpoint uses an LLM
    to resolve vague temporal/demonstrative references using contextual data from
    recent conversations, memories, and agents in the project. The response includes
    ``rewrite_info`` with the original/rewritten query and confidence.
    """
    t0 = time.monotonic()
    settings = get_settings()
    query_text = q.strip()

    # ── Query Rewriting ──────────────────────────────────────────────────────
    rewrite_info: QueryRewriteInfo | None = None
    search_query = query_text

    if rewrite and len(query_text) >= 2:
        try:
            from mneme.memory.query_rewriter import quick_rewrite

            rewrite_result = quick_rewrite(
                db, query=query_text, project_id=project_id, context=context,
            )

            if rewrite_result.is_rewritten and rewrite_result.confidence >= 0.5:
                search_query = rewrite_result.rewritten_query
                logger.info(
                    "Query rewritten: '%s' → '%s' (confidence=%.2f)",
                    query_text, search_query, rewrite_result.confidence,
                )
            else:
                logger.debug(
                    "Query not rewritten: '%s' (confidence=%.2f, is_rewritten=%s)",
                    query_text, rewrite_result.confidence, rewrite_result.is_rewritten,
                )

            rewrite_info = QueryRewriteInfo(
                original_query=query_text,
                rewritten_query=search_query,
                is_rewritten=rewrite_result.is_rewritten and search_query != query_text,
                confidence=rewrite_result.confidence,
                explanation=rewrite_result.explanation,
            )
        except Exception as exc:
            # Degrade gracefully — use original query if rewriting fails
            logger.warning("Query rewriting failed, using original query: %s", exc)
            rewrite_info = QueryRewriteInfo(
                original_query=query_text,
                rewritten_query=query_text,
                is_rewritten=False,
                confidence=0.0,
                explanation=f"Rewrite failed: {exc}",
            )

    like_pattern = f"%{search_query}%"

    # Track per-source counts for the response
    source_counts: dict[str, int] = {
        "agent": 0, "knowledge": 0, "memory": 0,
        "ppr_graph": 0, "temporal_cluster": 0,
    }

    # ── Agent search ───────────────────────────────────────────────────
    agent_sql = text("""
        SELECT
            agent_id AS result_id,
            name AS title,
            COALESCE(description, '') AS raw_text,
            0.6 AS rank
        FROM agents
        WHERE status = 'active'
          AND (
              LOWER(name) LIKE LOWER(:pattern)
              OR LOWER(COALESCE(description, '')) LIKE LOWER(:pattern)
          )
          AND (:project_id IS NULL OR project_id = :project_id)
        ORDER BY name ASC
        LIMIT :limit
    """)

    agent_rows = db.execute(
        agent_sql,
        {
            "pattern": like_pattern,
            "project_id": project_id,
            "limit": _MAX_PER_CATEGORY,
        },
    ).mappings().all()

    agent_results: list[GlobalSearchResult] = []
    for row in agent_rows:
        raw = row["raw_text"] or ""
        agent_results.append(
            GlobalSearchResult(
                source="agent",
                result_id=str(row["result_id"]),
                title=row["title"] or "",
                snippet=_snippet(raw, search_query),
                rank=float(row["rank"]),
                icon="bot",
                link=f"{_AGENT_LINK_PREFIX}",
                meta={"status": "active"},
            )
        )
    source_counts["agent"] = len(agent_results)

    # ── Knowledge search ──────────────────────────────────────────────
    knowledge_sql = text("""
        SELECT
            kc.chunk_id AS result_id,
            kd.title AS doc_title,
            kc.chunk_text AS raw_text,
            CAST(0.5 + random() * 0.1 AS float) AS rank,
            kd.document_id,
            kd.sensitivity_level,
            kd.document_status
        FROM knowledge_chunks kc
        JOIN knowledge_documents kd ON kd.document_id = kc.document_id
        WHERE kd.document_status = 'active'
          AND LOWER(kc.chunk_text) LIKE LOWER(:pattern)
          AND (:project_id IS NULL OR kd.project_id = :project_id)
        ORDER BY kc.chunk_order ASC
        LIMIT :limit
    """)

    knowledge_rows = db.execute(
        knowledge_sql,
        {
            "pattern": like_pattern,
            "project_id": project_id,
            "limit": _MAX_PER_CATEGORY,
        },
    ).mappings().all()

    knowledge_results: list[GlobalSearchResult] = []
    for row in knowledge_rows:
        raw = row["raw_text"] or ""
        knowledge_results.append(
            GlobalSearchResult(
                source="knowledge",
                result_id=str(row["result_id"]),
                title=row["doc_title"] or "",
                snippet=_snippet(raw, search_query),
                rank=float(row["rank"]),
                icon="book-open",
                link=f"{_KNOWLEDGE_LINK_PREFIX}",
                meta={
                    "document_id": str(row["document_id"]),
                    "sensitivity_level": row["sensitivity_level"] or "",
                    "status": row["document_status"],
                },
            )
        )
    source_counts["knowledge"] = len(knowledge_results)

    # ── Memory search ─────────────────────────────────────────────────
    memory_sql = text("""
        SELECT
            memory_id AS result_id,
            COALESCE(title, canonical_key) AS mem_title,
            memory_text AS raw_text,
            CAST(0.4 + random() * 0.1 AS float) AS rank,
            canonical_key,
            sensitivity_level,
            status,
            project_id AS mem_project_id
        FROM memories
        WHERE status IN ('active', 'draft')
          AND (
              LOWER(COALESCE(title, '')) LIKE LOWER(:pattern)
              OR LOWER(memory_text) LIKE LOWER(:pattern)
          )
          AND (:project_id IS NULL OR project_id = :project_id)
        ORDER BY updated_at DESC
        LIMIT :limit
    """)

    memory_rows = db.execute(
        memory_sql,
        {
            "pattern": like_pattern,
            "project_id": project_id,
            "limit": _MAX_PER_CATEGORY,
        },
    ).mappings().all()

    memory_results: list[GlobalSearchResult] = []
    seed_weights: dict[UUID, float] = {}
    for row in memory_rows:
        raw = row["raw_text"] or ""
        rank_val = float(row["rank"])
        memory_results.append(
            GlobalSearchResult(
                source="memory",
                result_id=str(row["result_id"]),
                title=row["mem_title"] or "",
                snippet=_snippet(raw, search_query),
                rank=rank_val,
                icon="brain",
                link=f"{_MEMORY_LINK_PREFIX}",
                meta={
                    "canonical_key": row["canonical_key"],
                    "sensitivity_level": row["sensitivity_level"] or "",
                    "status": row["status"],
                },
            )
        )
        # Collect seeds for PPR graph traversal
        seed_weights[row["result_id"]] = rank_val
    source_counts["memory"] = len(memory_results)

    # ── PPR Graph Traversal Recall ─────────────────────────────────────
    ppr_results: list[GlobalSearchResult] = []
    if settings.ppr_search_enabled and seed_weights:
        try:
            from mneme.memory.ppr_traversal import run_ppr_recall

            ppr_recall = run_ppr_recall(
                db,
                seed_memory_ids=seed_weights,
                top_k=settings.ppr_top_k,
                alpha=settings.ppr_teleport_alpha,
                project_id=project_id,
            )

            for mem in ppr_recall.node_details:
                ppr_score = float(mem.get("ppr_score", 0.0))
                # Scale PPR score into similar range as ILIKE rank (0.3-0.5 range)
                normalized_rank = 0.3 + ppr_score * 0.25
                raw_text = mem.get("memory_text") or ""
                ppr_results.append(
                    GlobalSearchResult(
                        source="ppr_graph",
                        result_id=str(mem["memory_id"]),
                        title=(mem.get("title") or mem.get("canonical_key") or ""),
                        snippet=_snippet(raw_text, search_query),
                        rank=round(normalized_rank, 4),
                        icon=_PPR_ICON,
                        link=f"{_MEMORY_LINK_PREFIX}",
                        meta={
                            "ppr_score": round(ppr_score, 6),
                            "canonical_key": mem.get("canonical_key"),
                            "sensitivity_level": mem.get("sensitivity_level") or "",
                            "status": mem.get("status"),
                            "discovery": "graph_traversal",
                        },
                    )
                )

            logger.info(
                "PPR recall: %d seeds → %d discovered (%.1fms)",
                ppr_recall.seed_count, ppr_recall.ppr_discovered_count,
                ppr_recall.elapsed_ms,
            )
        except Exception as exc:
            logger.warning("PPR graph traversal failed, skipping: %s", exc)

    source_counts["ppr_graph"] = len(ppr_results)

    # ── Temporal Shape Clustering ──────────────────────────────────────
    temporal_results: list[GlobalSearchResult] = []
    if settings.temporal_cluster_enabled:
        try:
            from mneme.memory.temporal_cluster import temporal_cluster_search

            tc_result = temporal_cluster_search(
                db,
                query=query_text,
                project_id=project_id,
                top_k=settings.temporal_cluster_top_k,
            )

            if tc_result.has_temporal_match:
                # Track existing result IDs to avoid duplicates
                existing_ids = {
                    r.result_id for r in memory_results + ppr_results
                }

                # Add concept clusters as grouped results
                for cluster in tc_result.clusters:
                    cluster_mems = cluster.get("memories", [])
                    concept_label = cluster.get("concept_label", "temporal")
                    cluster_weight = float(cluster.get("weight", 0.4))

                    for mem in cluster_mems:
                        mid = mem["memory_id"]
                        str_mid = str(mid) if not isinstance(mid, str) else mid
                        if str_mid in existing_ids:
                            continue
                        existing_ids.add(str_mid)

                        temporal_score = float(mem.get("temporal_score", 0.5))
                        temporal_rank = cluster_weight * temporal_score
                        raw_text = mem.get("memory_text") or ""
                        temporal_results.append(
                            GlobalSearchResult(
                                source="temporal_cluster",
                                result_id=str_mid,
                                title=(mem.get("title") or mem.get("canonical_key") or ""),
                                snippet=_snippet(raw_text, search_query),
                                rank=round(0.3 + temporal_rank * 0.3, 4),
                                icon=_TEMPORAL_ICON,
                                link=f"{_MEMORY_LINK_PREFIX}",
                                meta={
                                    "concept_label": concept_label,
                                    "temporal_expression": mem.get("temporal_expression"),
                                    "temporal_score": temporal_score,
                                    "canonical_key": mem.get("canonical_key"),
                                    "sensitivity_level": mem.get("sensitivity_level") or "",
                                    "status": mem.get("status"),
                                    "discovery": "temporal_cluster",
                                },
                            )
                        )

            logger.info(
                "Temporal cluster: %d ranges → %d memories → %d clusters → %d unique results (%.1fms)",
                len(tc_result.temporal_expressions), tc_result.memories_found,
                len(tc_result.clusters), len(temporal_results), tc_result.elapsed_ms,
            )
        except Exception as exc:
            logger.warning("Temporal cluster search failed, skipping: %s", exc)

    source_counts["temporal_cluster"] = len(temporal_results)

    # ── Merge & cross-encoder re-rank (P5-03) ─────────────────────────
    all_results = (
        agent_results + knowledge_results + memory_results +
        ppr_results + temporal_results
    )

    # Build RerankCandidate objects for cross-encoder re-ranking
    # Combine result_id, snippet, and source for the cross-encoder text
    candidate_index: list[int] = []  # index → position in all_results
    rerank_candidates: list[RerankCandidate] = []
    for i, r in enumerate(all_results):
        # Build a representative text for each result
        candidate_text = f"{r.title}\n{r.snippet}".strip()
        if not candidate_text:
            candidate_text = r.title or f"{r.source} result"
        rerank_candidates.append(RerankCandidate(
            id=str(i),
            text=candidate_text,
            fts_score=r.rank,
            vector_score=0.0,
            meta={"source": r.source},
        ))
        candidate_index.append(i)

    # Apply cross-encoder reranking if we have candidates
    rerank_applied = False
    if rerank_candidates and len(rerank_candidates) > 1:
        reranker = get_reranker()
        try:
            reranked = reranker.rerank(
                query=search_query,
                candidates=rerank_candidates,
                project_id=project_id,
                context=context,
            )
            # Map reranker scores back to results
            score_map: dict[int, float] = {}
            for rr in reranked:
                try:
                    idx = int(rr.id)
                    score_map[idx] = rr.score
                except (ValueError, KeyError):
                    continue

            if score_map:
                for idx, r in enumerate(all_results):
                    if idx in score_map:
                        r.rank = score_map[idx]
                rerank_applied = True
                logger.debug("Global search reranked %d results via cross-encoder", len(score_map))
        except RerankerError as exc:
            logger.warning("Global search reranker failed, using heuristic sort: %s", exc)

    all_results.sort(key=lambda r: r.rank, reverse=True)

    total = len(all_results)

    # Paginate
    offset = (page - 1) * page_size
    paged = all_results[offset : offset + page_size]
    total_pages = max(1, (total + page_size - 1) // page_size) if total > 0 else 0

    elapsed_ms = (time.monotonic() - t0) * 1000.0

    data = GlobalSearchResponse(
        items=paged,
        total=total,
        query_time_ms=round(elapsed_ms, 2),
        source_counts=source_counts,
        rewrite_info=rewrite_info,
        rerank_applied=rerank_applied,
    )

    return envelope(
        jsonable_encoder(data.model_dump(mode="json")),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )
