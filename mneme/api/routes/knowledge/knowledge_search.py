"""Knowledge FTS search + citation endpoints (P3-07 / P3-08 / P5-03).

Endpoints
---------
* ``GET /api/v4/knowledge/search`` — Full-text search across chunks (with stale markers
  and optional context-window expansion).
* ``GET /api/v4/knowledge/documents/{id}/index-state`` — Read index_states
* ``POST /api/v4/knowledge/indexes/refresh`` — Refresh stale FTS indexes
* ``GET /api/v4/knowledge/citations/{chunk_id}`` — Build single citation (P3-08)
* ``GET /api/v4/knowledge/documents/{id}/citations`` — List document citations (P3-08)
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.encoders import jsonable_encoder
from sqlalchemy.orm import Session

from mneme.api.context import RequestContext, get_request_context
from mneme.api.errors import ApiError
from mneme.api.routes.knowledge.knowledge import _require_document
from mneme.api.schemas import envelope
from mneme.db.base import get_db
from mneme.knowledge.citation import (
    build_citation,
    check_stale_documents,
    list_citations,
)
from mneme.knowledge.fts import (
    ensure_fts_index,
    get_index_state,
    refresh_stale_fts_indexes,
    search_fts,
)
from mneme.schemas import (
    CitationListResponse,
    CitationRead,
    IndexStateRead,
    KnowledgeFtsSearchResult,
    PageInfo,
    PaginatedData,
    ResponseEnvelope,
)
from mneme.search.context_window import (
    ContextWindowConfig,
    expand_multiple_chunks,
)

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


# ═══════════════════════════════════════════════════════════════════
# GET /api/v4/knowledge/search — Full-text search
# ═══════════════════════════════════════════════════════════════════

@router.get("/search", response_model=ResponseEnvelope[PaginatedData[KnowledgeFtsSearchResult]])
def search_knowledge_route(
    q: str = Query(..., min_length=1, max_length=500, description="Search query"),
    project_id: UUID | None = Query(None, description="Filter by project"),
    sensitivity_floor: str | None = Query(
        None, description="Minimum sensitivity (public/normal/private/sensitive/secret)"
    ),
    expand_context: bool = Query(
        False, description="Expand context: surrounding paragraphs + related memories"
    ),
    context_radius: int = Query(
        1, ge=0, le=3, description="Number of surrounding chunks before/after match"
    ),
    context_related_memories: int = Query(
        3, ge=0, le=10, description="Max related memories to include per result"
    ),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Results per page"),
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Full-text search across all knowledge chunks.

    Searches ``knowledge_chunks.chunk_text`` using PostgreSQL FTS with
    the ``simple`` text-search config (no stemming — works for Chinese + English).

    Results are ranked by ``ts_rank`` and include document/block context.
    Each result includes ``is_stale`` / ``stale_reason`` markers when the
    document's index is out of date.

    When ``expand_context=True``, each result is enriched with:
    * ``surrounding_chunks`` — neighboring chunks in the same document (±radius).
    * ``related_memories`` — memory entries whose key terms overlap with the chunk.

    Only active documents are searched.
    """
    # Ensure the GIN index exists (idempotent)
    ensure_fts_index(db)

    results, total = search_fts(
        db,
        query=q.strip(),
        project_id=project_id,
        sensitivity_floor=sensitivity_floor,
        page=page,
        page_size=page_size,
    )

    # Augment results with stale markers (P3-08)
    if results:
        doc_ids = list({r.document_id for r in results})
        stale_states = check_stale_documents(db, document_ids=doc_ids)
        for r in results:
            state = stale_states.get(r.document_id)
            if state:
                fts_state, _citation_state = state
                if fts_state != "ready":
                    r.is_stale = True
                    r.stale_reason = f"fts_{fts_state}"

    # Context window expansion (P5-03)
    if expand_context and results:
        chunk_info = [
            {
                "chunk_id": r.chunk_id,
                "document_id": r.document_id,
                "chunk_order": r.chunk_order,
                "chunk_text": r.chunk_text,
            }
            for r in results
        ]
        cfg = ContextWindowConfig(
            surrounding_radius=context_radius,
            max_related_memories=context_related_memories,
        )
        expanded = expand_multiple_chunks(
            db,
            chunk_info=chunk_info,
            project_id=project_id,
            config=cfg,
        )
        # Merge expanded context back into results
        for r in results:
            ctx_result = expanded.get(r.chunk_id)
            if ctx_result is None:
                continue
            r.surrounding_chunks = [
                {
                    "chunk_id": str(sc.chunk_id),
                    "chunk_order": sc.chunk_order,
                    "chunk_text": sc.chunk_text,
                    "token_count": sc.token_count,
                    "relative_position": sc.relative_position,
                }
                for sc in ctx_result.surrounding_chunks
            ]
            r.related_memories = [
                {
                    "memory_id": str(rm.memory_id),
                    "title": rm.title,
                    "memory_text_preview": rm.memory_text_preview,
                    "canonical_key": rm.canonical_key,
                    "relevance_score": rm.relevance_score,
                }
                for rm in ctx_result.related_memories
            ]

    total_pages = max(1, (total + page_size - 1) // page_size) if total > 0 else 0

    paginated = PaginatedData(
        items=results,
        page_info=PageInfo(
            page=page,
            page_size=page_size,
            total_items=total,
            total_pages=total_pages,
            has_next=page < total_pages,
            has_previous=page > 1,
        ),
    )

    data = jsonable_encoder(paginated)
    return envelope(data, request_id=context.request_id, correlation_id=context.correlation_id)


# ═══════════════════════════════════════════════════════════════════
# GET /api/v4/knowledge/documents/{document_id}/index-state
# ═══════════════════════════════════════════════════════════════════

@router.get(
    "/documents/{document_id}/index-state",
    response_model=ResponseEnvelope[IndexStateRead],
)
def get_document_index_state_route(
    document_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Read the index_states row for a knowledge document.

    Returns the current state of FTS, vector, graph, and citation indexes.
    """
    _require_document(db, document_id)

    state = get_index_state(db, document_id=document_id)
    if state is None:
        raise ApiError(404, "not_found", f"索引状态不存在: {document_id}")

    data = jsonable_encoder(state)
    return envelope(data, request_id=context.request_id, correlation_id=context.correlation_id)


# ═══════════════════════════════════════════════════════════════════
# POST /api/v4/knowledge/indexes/refresh — Refresh stale FTS indexes
# ═══════════════════════════════════════════════════════════════════

@router.post("/indexes/refresh", response_model=ResponseEnvelope[dict])
def refresh_fts_indexes_route(
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Refresh FTS index states for documents marked as stale.

    Since the actual GIN index is a live expression index on
    ``knowledge_chunks.chunk_text``, this simply transitions
    ``fts_state`` from ``'stale'`` to ``'ready'``.

    Returns the number of documents refreshed.
    """
    count = refresh_stale_fts_indexes(db)
    db.commit()

    data = jsonable_encoder({"refreshed": count})
    return envelope(
        data,
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════
# P3-08: Citation endpoints
# ═══════════════════════════════════════════════════════════════════


@router.get(
    "/citations/{chunk_id}",
    response_model=ResponseEnvelope[CitationRead],
)
def get_chunk_citation_route(
    chunk_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Build a single citation for a chunk.

    Returns the full provenance chain from the chunk back to the source asset,
    including staleness information from ``index_states``.
    """
    citation = build_citation(db, chunk_id=chunk_id)
    if citation is None:
        raise ApiError(404, "not_found", f"分块 {chunk_id} 不存在")

    data = jsonable_encoder(citation)
    return envelope(data, request_id=context.request_id, correlation_id=context.correlation_id)


@router.get(
    "/documents/{document_id}/citations",
    response_model=ResponseEnvelope[CitationListResponse],
)
def list_document_citations_route(
    document_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """List all citations for every chunk in a document.

    Each citation includes the full provenance chain (chunk → block →
    document → asset) and staleness status from ``index_states``.
    """
    _require_document(db, document_id)

    result = list_citations(db, document_id=document_id)
    data = jsonable_encoder(result)
    return envelope(data, request_id=context.request_id, correlation_id=context.correlation_id)
