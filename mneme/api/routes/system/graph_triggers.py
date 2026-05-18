"""L7-03 Memory Dependency Graph Triggers — API.

Route prefix: ``/graph-triggers``

Provides endpoints to:
* Query the graph trigger audit log.
* Find memory dependents via graph edges.
* Trigger manual backfill/repair.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from mneme.api.auth import get_current_user_session
from mneme.api.context import RequestContext, get_request_context
from mneme.db.auth import AuthenticatedSession
from mneme.db.base import get_db
from mneme.db.graph_triggers import (
    list_trigger_log,
    find_memory_dependents,
    backfill_memory_to_graph,
    backfill_all_active_memories,
)
from mneme.schemas.events import GraphTriggerLogEntryRead

router = APIRouter(prefix="/graph-triggers", tags=["graph-triggers"])


@router.get("/log", response_model=dict)
def get_trigger_log(
    memory_id: UUID | None = None,
    trigger_event: str | None = None,
    action: str | None = None,
    page: int = 1,
    page_size: int = 50,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    _auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Read memory→graph sync trigger log entries."""
    items, total = list_trigger_log(
        db,
        memory_id=memory_id,
        trigger_event=trigger_event,
        action=action,
        page=page,
        page_size=page_size,
    )
    return {
        "items": [item.model_dump(mode="json") for item in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/dependents/{memory_id}", response_model=dict)
def get_memory_dependents(
    memory_id: UUID,
    max_results: int = 50,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    _auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Find graph-dependent nodes for a given memory."""
    dependents = find_memory_dependents(db, memory_id=memory_id, max_results=max_results)
    return {
        "memory_id": str(memory_id),
        "dependents": dependents,
        "count": len(dependents),
    }


@router.post("/backfill/{memory_id}", response_model=dict)
def trigger_backfill_single(
    memory_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    _auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Manually sync a single memory to graph_nodes."""
    return backfill_memory_to_graph(db, memory_id=memory_id)


@router.post("/backfill", response_model=dict)
def trigger_backfill_all(
    batch_size: int = 100,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    _auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Backfill all active memories into graph_nodes (one-time repair)."""
    return backfill_all_active_memories(db, batch_size=batch_size)
