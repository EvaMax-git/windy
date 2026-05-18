"""Source Map API routes — provenance mapping endpoints for /api/v4/source-maps.

The Source Map routes provide CRUD access to the ``source_maps`` table,
which is the backbone of Mneme's provenance tracking system.  Each source
map records a directional link from a source object (asset, document, block,
chunk, message, etc.) to a target object (document, block, chunk, memory, etc.)
with a semantic ``mapping_role``.

Endpoints
---------
* ``POST   /api/v4/source-maps`` — Create a source→target mapping
* ``GET    /api/v4/source-maps`` — List source maps (paginated, filterable)
* ``GET    /api/v4/source-maps/upstream`` — Find upstream sources for a target
* ``GET    /api/v4/source-maps/downstream`` — Find downstream targets for a source
* ``GET    /api/v4/source-maps/{source_map_id}`` — Get a single source map
* ``DELETE /api/v4/source-maps/{source_map_id}`` — Delete a source map

Every write endpoint requires an ``Idempotency-Key`` header.
"""

from __future__ import annotations

import math
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.encoders import jsonable_encoder
from sqlalchemy.orm import Session

from mneme.api.auth import get_current_user_session
from mneme.api.context import (
    ActorContext,
    RequestContext,
    get_request_context,
    with_actor,
)
from mneme.api.errors import ApiError
from mneme.api.schemas import envelope
from mneme.db.auth import AuthenticatedSession
from mneme.db.base import get_db
from mneme.db.source_maps import (
    create_source_map,
    delete_source_map,
    get_source_map,
    list_downstream,
    list_source_maps,
    list_upstream,
)
from mneme.schemas import (
    PageInfo,
    PaginatedData,
    ResponseEnvelope,
    SourceMapCreate,
    SourceMapRead,
)

router = APIRouter(prefix="/source-maps", tags=["source_maps"])


# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════

def _build_paginated_response(
    items: list,
    total: int,
    page: int,
    page_size: int,
) -> PaginatedData:
    """Construct a PaginatedData wrapper with computed page metadata."""
    total_pages = max(1, math.ceil(total / page_size)) if total > 0 else 1
    return PaginatedData(
        items=items,
        page_info=PageInfo(
            page=page,
            page_size=page_size,
            total_items=total,
            total_pages=total_pages,
            has_next=page < total_pages,
            has_previous=page > 1,
        ),
    )


def _resolve_actor(
    auth: AuthenticatedSession, context: RequestContext
) -> RequestContext:
    """Wire the authenticated user into the request context's actor."""
    return with_actor(
        context,
        actor=ActorContext(
            actor_type="user",
            actor_id=auth.user.user_id,
            auth_context_type="user_session",
            auth_context_id=auth.session.session_id,
        ),
    )


# ═══════════════════════════════════════════════════════════════════════
# POST /api/v4/source-maps — Create a source map
# ═══════════════════════════════════════════════════════════════════════

@router.post("", response_model=ResponseEnvelope[SourceMapRead], status_code=201)
def create_source_map_route(
    payload: SourceMapCreate,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Create a new source→target provenance mapping.

    Links a source object (asset, document, block, chunk, etc.) to a
    target object with a semantic ``mapping_role`` (citation, derived_from,
    extracted_from, transformed_from, or attachment).

    Requires ``Idempotency-Key`` header for safe retry.
    """
    if not context.idempotency_key:
        raise ApiError(
            400, "bad_request", "Idempotency-Key 头必须存在"
        )

    context = _resolve_actor(auth, context)

    sm = create_source_map(
        db,
        project_id=payload.project_id,
        source_type=payload.source_type,
        source_id=payload.source_id,
        target_type=payload.target_type,
        target_id=payload.target_id,
        source_asset_id=payload.source_asset_id,
        source_document_id=payload.source_document_id,
        source_block_id=payload.source_block_id,
        target_document_id=payload.target_document_id,
        target_block_id=payload.target_block_id,
        target_chunk_id=payload.target_chunk_id,
        span=payload.span,
        confidence=payload.confidence,
        mapping_role=payload.mapping_role,
    )

    return envelope(
        jsonable_encoder(sm),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════
# GET /api/v4/source-maps — List source maps
# ═══════════════════════════════════════════════════════════════════════

@router.get("", response_model=ResponseEnvelope[PaginatedData[SourceMapRead]])
def list_source_maps_route(
    project_id: UUID | None = Query(None, description="按项目过滤"),
    source_type: str | None = Query(
        None,
        description="源类型: asset|document|block|chunk|message|raw_event|memory_candidate|external",
    ),
    target_type: str | None = Query(
        None,
        description="目标类型: document|block|chunk|memory_candidate|memory|asset",
    ),
    mapping_role: str | None = Query(
        None,
        description="映射角色: citation|derived_from|extracted_from|transformed_from|attachment",
    ),
    source_id: UUID | None = Query(None, description="按源对象ID过滤"),
    target_id: UUID | None = Query(None, description="按目标对象ID过滤"),
    page: int = Query(1, ge=1, description="页码 (从1开始)"),
    page_size: int = Query(50, ge=1, le=200, description="每页数量"),
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """List source maps with optional filters and pagination.

    Supports filtering by project, source/target type, mapping role,
    and individual source/target IDs.
    """
    items, total = list_source_maps(
        db,
        project_id=project_id,
        source_type=source_type,
        target_type=target_type,
        mapping_role=mapping_role,
        source_id=source_id,
        target_id=target_id,
        page=page,
        page_size=page_size,
    )

    result = _build_paginated_response(items, total, page, page_size)
    return envelope(
        jsonable_encoder(result),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════
# GET /api/v4/source-maps/upstream — Upstream provenance
# ═══════════════════════════════════════════════════════════════════════
# NOTE: literal paths (/upstream, /downstream) MUST be registered before
# the parameterized /{source_map_id} route, otherwise FastAPI will attempt
# to parse "upstream" as a UUID and return 422 instead of falling through.

@router.get("/upstream", response_model=ResponseEnvelope[PaginatedData[SourceMapRead]])
def list_upstream_route(
    target_type: str = Query(
        ...,
        description="目标类型: document|block|chunk|memory_candidate|memory|asset",
    ),
    target_id: UUID = Query(..., description="目标对象ID"),
    mapping_role: str | None = Query(
        None,
        description="可选: 按映射角色过滤",
    ),
    page: int = Query(1, ge=1, description="页码 (从1开始)"),
    page_size: int = Query(50, ge=1, le=200, description="每页数量"),
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Find all sources that point TO a given target (upstream provenance).

    Answers: "Where did this target come from?"

    Use this endpoint to trace the origin of a document, block, chunk,
    memory, or any other target type.
    """
    items, total = list_upstream(
        db,
        target_type=target_type,
        target_id=target_id,
        mapping_role=mapping_role,
        page=page,
        page_size=page_size,
    )

    result = _build_paginated_response(items, total, page, page_size)
    return envelope(
        jsonable_encoder(result),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════
# GET /api/v4/source-maps/downstream — Downstream provenance
# ═══════════════════════════════════════════════════════════════════════

@router.get("/downstream", response_model=ResponseEnvelope[PaginatedData[SourceMapRead]])
def list_downstream_route(
    source_type: str = Query(
        ...,
        description="源类型: asset|document|block|chunk|message|raw_event|memory_candidate|external",
    ),
    source_id: UUID = Query(..., description="源对象ID"),
    mapping_role: str | None = Query(
        None,
        description="可选: 按映射角色过滤",
    ),
    page: int = Query(1, ge=1, description="页码 (从1开始)"),
    page_size: int = Query(50, ge=1, le=200, description="每页数量"),
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Find all targets that originate FROM a given source (downstream derived).

    Answers: "What was derived from this source?"

    Use this endpoint to discover all downstream artifacts (documents,
    blocks, chunks, memories, etc.) that were produced from a given source.
    """
    items, total = list_downstream(
        db,
        source_type=source_type,
        source_id=source_id,
        mapping_role=mapping_role,
        page=page,
        page_size=page_size,
    )

    result = _build_paginated_response(items, total, page, page_size)
    return envelope(
        jsonable_encoder(result),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════
# GET /api/v4/source-maps/{source_map_id} — Get a single source map
# ═══════════════════════════════════════════════════════════════════════

@router.get("/{source_map_id}", response_model=ResponseEnvelope[SourceMapRead])
def get_source_map_route(
    source_map_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Get a single source map by its primary key."""
    sm = get_source_map(db, source_map_id)
    if sm is None:
        raise ApiError(
            404, "not_found", f"source_map {source_map_id} 不存在"
        )

    return envelope(
        jsonable_encoder(sm),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════
# DELETE /api/v4/source-maps/{source_map_id} — Delete a source map
# ═══════════════════════════════════════════════════════════════════════

@router.delete("/{source_map_id}", response_model=ResponseEnvelope[dict])
def delete_source_map_route(
    source_map_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Delete a source map entry.

    This is a hard delete — the mapping row is permanently removed.
    Requires ``Idempotency-Key`` header.
    """
    if not context.idempotency_key:
        raise ApiError(
            400, "bad_request", "Idempotency-Key 头必须存在"
        )

    sm = get_source_map(db, source_map_id)
    if sm is None:
        raise ApiError(
            404, "not_found", f"source_map {source_map_id} 不存在"
        )

    deleted = delete_source_map(db, source_map_id)

    return envelope(
        {"deleted": deleted, "source_map_id": str(source_map_id)},
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )
