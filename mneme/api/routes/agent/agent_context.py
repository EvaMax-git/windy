"""P8-01 Agent Context endpoint — delegates to context assembly engine.

Endpoint
--------
POST /agent/context  — Assemble context for an agent query
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.encoders import jsonable_encoder
from sqlalchemy.orm import Session

from mneme.api.auth import get_current_user_session
from mneme.api.context import RequestContext, get_request_context
from mneme.api.schemas import envelope
from mneme.context.assembly_engine import assemble_context
from mneme.db.auth import AuthenticatedSession
from mneme.db.base import get_db
from mneme.schemas.common import ResponseEnvelope
from mneme.schemas.context_assembly import (
    AssembleRequest,
    AssembleResponse,
    BudgetBreakdown,
    CardSection,
)

router = APIRouter(prefix="/agent", tags=["agent"])


@router.post(
    "/context",
    response_model=ResponseEnvelope[AssembleResponse],
    status_code=200,
)
def agent_context_route(
    payload: AssembleRequest,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_request_context),
    _auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Assemble context for an agent query using card-based injection strategies.

    Convenience route that delegates to the context assembly engine.
    """
    result = assemble_context(
        db,
        ctx,
        agent_id=payload.agent_id,
        query_text=payload.query_text,
        project_id=payload.project_id,
        conversation_history=payload.conversation_history,
        max_tokens=payload.max_tokens,
        strategy_overrides=payload.strategy_overrides,
        expand_cards=payload.expand_cards,
    )

    response = AssembleResponse(
        agent_id=payload.agent_id,
        assembled_text=result["assembled_text"],
        sections=[CardSection(**s) for s in result["sections"]],
        budget=BudgetBreakdown(**result["budget"]),
        total_tokens=result["total_tokens"],
        strategy_summary=result["strategy_summary"],
        degradation_reason=result["degradation_reason"],
    )

    return envelope(
        jsonable_encoder(response),
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
    )
