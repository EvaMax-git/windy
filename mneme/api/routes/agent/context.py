"""P5-04 Context Compiler API routes.

Endpoints
---------
POST /context/compile       — Compile a context pack
GET  /context/packs         — List context packs
GET  /context/packs/{id}    — Get context pack detail with items
"""

from __future__ import annotations

from math import ceil
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.encoders import jsonable_encoder
from sqlalchemy.orm import Session

from mneme.api.auth import get_current_user_session
from mneme.api.context import RequestContext, get_request_context
from mneme.api.errors import ApiError
from mneme.api.schemas import envelope
from mneme.context.compiler import compile_context
from mneme.db.auth import AuthenticatedSession
from mneme.db.base import get_db
from mneme.db.context_packs import (
    get_context_pack,
    get_context_pack_items,
    list_context_packs,
)
from mneme.schemas.common import PageInfo, ResponseEnvelope
from mneme.schemas.context import (
    CompileRequest,
    CompileResponse,
    ContextPackDetailRead,
    ContextPackListResponse,
    ContextPackRead,
)

router = APIRouter(prefix="/context", tags=["context"])


@router.post("/compile", response_model=ResponseEnvelope[CompileResponse], status_code=201)
def compile_route(
    payload: CompileRequest,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    _auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Compile a context pack for the given query.

    Retrieves relevant knowledge and memories, ranks them by relevance,
    applies sensitivity ceiling filtering and token budget trimming,
    and writes the result as a context_packs + context_pack_items record.
    """
    result = compile_context(
        db,
        context,
        agent_id=payload.agent_id,
        project_id=payload.project_id,
        query_text=payload.query_text,
        compile_mode=payload.compile_mode.value,
        token_budget=payload.token_budget.model_dump(),
        sensitivity_ceiling=payload.sensitivity_ceiling.value,
    )

    pack = result["pack"]
    items = result["items"]

    detail = ContextPackDetailRead(
        **pack,
        items=items,
    )

    response = CompileResponse(
        context_pack=detail,
        total_token_count=result["total_token_count"],
        included_count=result["included_count"],
        excluded_count=result["excluded_count"],
        degradation_reason=result["degradation_reason"],
    )

    return envelope(
        jsonable_encoder(response),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.get("/packs", response_model=ResponseEnvelope[ContextPackListResponse])
def list_packs_route(
    agent_id: UUID | None = None,
    project_id: UUID | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 50,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    _auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """List context packs with optional filters and pagination."""
    packs, total = list_context_packs(
        db,
        agent_id=agent_id,
        project_id=project_id,
        status=status,
        page=page,
        page_size=page_size,
    )

    data = ContextPackListResponse(
        items=packs,
        page_info=PageInfo(
            page=page,
            page_size=page_size,
            total_items=total,
            total_pages=ceil(total / page_size) if total > 0 else 0,
            has_next=(page * page_size) < total,
            has_previous=page > 1,
        ),
    )
    return envelope(
        jsonable_encoder(data),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.get("/packs/{pack_id}", response_model=ResponseEnvelope[ContextPackDetailRead])
def get_pack_route(
    pack_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    _auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Get a context pack detail with all its items."""
    pack = get_context_pack(db, pack_id)
    if pack is None:
        raise ApiError(404, "bad_request", f"Context pack not found: {pack_id}")

    items = get_context_pack_items(db, pack_id)

    detail = ContextPackDetailRead(**pack, items=items)
    return envelope(
        jsonable_encoder(detail),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )
