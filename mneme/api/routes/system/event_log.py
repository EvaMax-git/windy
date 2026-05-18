"""L7-01 Event Sourcing — event_log query/replay API.

Route prefix: ``/event-log``

The ``event_log`` is **append-only** — this module only provides read endpoints.
Writing must go through ``mneme.db.event_log.append_event_log()``.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from mneme.api.auth import get_current_user_session
from mneme.api.context import RequestContext, get_request_context
from mneme.db.auth import AuthenticatedSession
from mneme.db.base import get_db
from mneme.db.event_log import read_stream, list_event_log
from mneme.schemas.events import (
    EventLogFilterParams,
    EventLogListResponse,
)

router = APIRouter(prefix="/event-log", tags=["event-sourcing"])


@router.get("/stream/{stream_type}/{stream_id}", response_model=dict)
def replay_stream(
    stream_type: str,
    stream_id: UUID,
    after_version: int = 0,
    page: int = 1,
    page_size: int = 100,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    _auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Replay events for a given stream, ordered by version ascending."""
    items, total = read_stream(
        db,
        stream_type=stream_type,
        stream_id=stream_id,
        after_version=after_version,
        page=page,
        page_size=page_size,
    )
    return {
        "items": [item.model_dump(mode="json") for item in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/search", response_model=EventLogListResponse)
def search_event_log(
    project_id: UUID | None = None,
    stream_type: str | None = None,
    event_type: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    page: int = 1,
    page_size: int = 50,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    _auth: AuthenticatedSession = Depends(get_current_user_session),
) -> EventLogListResponse:
    """Search event log entries with filters."""


    filters = EventLogFilterParams(
        project_id=project_id,
        stream_type=stream_type,
        event_type=event_type,
        since=since,
        until=until,
    )
    items, total = list_event_log(
        db,
        filters=filters,
        page=page,
        page_size=page_size,
    )
    return EventLogListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
    )
