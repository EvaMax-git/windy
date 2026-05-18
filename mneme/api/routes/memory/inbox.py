"""Inbox API routes.

Endpoints
---------
* ``POST /api/v4/inbox`` — Create an inbox item (text/url types).
* ``POST /api/v4/inbox/upload`` — Upload a file to the inbox (multipart/form-data).
* ``GET  /api/v4/inbox`` — List inbox items (paginated, filterable).
* ``GET  /api/v4/inbox/{inbox_item_id}`` — Get an inbox item's details.
* ``POST /api/v4/inbox/{inbox_item_id}/process`` — Trigger processing (advance status).

Every write endpoint requires an ``Idempotency-Key`` header and records
audit + outbox events in the same transaction.
"""

from __future__ import annotations

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
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
from mneme.db.idempotency import check_idempotency_key_any
from mneme.db.inbox import (
    create_inbox_from_staging,
    create_inbox_item,
    get_inbox_item,
    link_inbox_to_asset,
    list_inbox_items,
    lookup_inbox_by_hash,
    mark_inbox_processed,
    update_inbox_status,
)
from mneme.db.projects import get_project
from mneme.schemas import (
    InboxItemCreateRequest,
    InboxItemRead,
    InboxType,
    PaginatedData,
    PageInfo,
    ProjectRead,
    ResponseEnvelope,
    UploadRequest,
)
from mneme.storage.upload import (
    handle_idempotent_upload_stream,
    validate_filename,
)

router = APIRouter(prefix="/inbox", tags=["inbox"])


# ═══════════════════════════════════════════════════════════════════
# Request helpers
# ═══════════════════════════════════════════════════════════════════

def _require_project(db: Session, project_id: UUID) -> ProjectRead:
    """Fetch and validate that a project exists."""
    project = get_project(db, project_id)
    if project is None:
        raise ApiError(404, "not_found", f"项目 {project_id} 不存在")
    return project


def _guard_idempotency(db: Session, key: str, expected_type: str) -> None:
    """Reject a request if the idempotency key was used for a different aggregate type."""
    existing = check_idempotency_key_any(db, idempotency_key=key)
    if existing is None:
        return
    _event_id, actual_type, _aggregate_id = existing
    if actual_type != expected_type:
        raise ApiError(
            409,
            "idempotency_conflict",
            f"幂等键已被用于 '{actual_type}'，而非 '{expected_type}'",
            details={
                "expected_aggregate_type": expected_type,
                "existing_aggregate_type": actual_type,
            },
        )


def _resolve_actor(auth: AuthenticatedSession, context: RequestContext) -> RequestContext:
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


# ═══════════════════════════════════════════════════════════════════
# POST /api/v4/inbox — Create an inbox item (text/url types)
# ═══════════════════════════════════════════════════════════════════

@router.post("", response_model=ResponseEnvelope[InboxItemRead])
def create_inbox_route(
    payload: InboxItemCreateRequest,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Create an inbox item for non-file types (url, text, etc.).

    For file uploads, use ``POST /api/v4/inbox/upload`` instead.
    """
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "写请求必须提供 Idempotency-Key 头")

    _guard_idempotency(db, context.idempotency_key, "inbox_item")
    _require_project(db, payload.project_id)

    context = _resolve_actor(auth, context)

    inbox_item = create_inbox_item(
        db,
        context,
        payload=payload,
        status="received",
    )

    data = jsonable_encoder(inbox_item)
    return envelope(data, request_id=context.request_id, correlation_id=context.correlation_id)


# ═══════════════════════════════════════════════════════════════════
# POST /api/v4/inbox/upload — Upload a file to inbox
# ═══════════════════════════════════════════════════════════════════

@router.post("/upload", response_model=ResponseEnvelope[InboxItemRead])
async def upload_file_route(
    file: UploadFile = File(...),
    project_id: UUID = Form(...),
    title: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Upload a file to the inbox.

    The file is staged to disk, its content hash is computed, and an
    ``inbox_items`` row is created with ``status='staged'``.

    If a file with the same content hash already exists in the project,
    the existing inbox item is returned (idempotent upload).
    """
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "写请求必须提供 Idempotency-Key 头")

    _guard_idempotency(db, context.idempotency_key, "inbox_item")
    project = _require_project(db, project_id)
    context = _resolve_actor(auth, context)

    # Validate filename
    if not file.filename:
        raise ApiError(400, "bad_request", "文件名不能为空")

    try:
        validate_filename(file.filename)
    except ValueError as e:
        raise ApiError(400, "bad_request", str(e))

    # ── Stage the file via the storage layer ────────────────────────
    def lookup_dup(content_hash: str, pid: UUID | None) -> None:
        """Check for existing inbox item with same hash.

        Returns None if no duplicate found (proceed with staging).
        If a duplicate is found, we raise a special marker to signal
        the idempotent case.
        """
        existing = lookup_inbox_by_hash(
            db, content_hash=content_hash, project_id=pid
        )
        if existing is not None:
            # Signal duplicate by storing on the context-like object
            raise _DuplicateFound(existing)

    class _DuplicateFound(Exception):
        def __init__(self, item: InboxItemRead):
            self.item = item

    try:
        upload_result = handle_idempotent_upload_stream(
            stream=file.file,
            original_filename=file.filename,
            project_id=project_id,
            lookup_duplicate=lookup_dup,
        )
    except _DuplicateFound as dup:
        # Idempotent upload — return existing inbox item
        data = jsonable_encoder(dup.item)
        return envelope(
            data,
            request_id=context.request_id,
            correlation_id=context.correlation_id,
            meta={"duplicate": True},
        )

    # ── Create inbox item from staged file ──────────────────────────
    if upload_result.staged_info is None:
        raise ApiError(500, "internal_error", "文件暂存失败：staged_info 为空")

    inbox_item = create_inbox_from_staging(
        db,
        context,
        project_id=project_id,
        staged_info=upload_result.staged_info,
        title=title or file.filename,
        source="api",
        source_uri=f"file://{upload_result.staged_info.staging_path}",
    )

    data = jsonable_encoder(inbox_item)
    return envelope(data, request_id=context.request_id, correlation_id=context.correlation_id)


# ═══════════════════════════════════════════════════════════════════
# GET /api/v4/inbox — List inbox items
# ═══════════════════════════════════════════════════════════════════

@router.get("", response_model=ResponseEnvelope[PaginatedData[InboxItemRead]])
def list_inbox_route(
    project_id: Optional[UUID] = Query(None, description="按项目过滤"),
    status: Optional[str] = Query(None, description="按状态过滤 (received/staged/linked/processed)"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(50, ge=1, le=200, description="每页数量"),
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """List inbox items with optional filters and pagination."""
    items, total = list_inbox_items(
        db,
        project_id=project_id,
        status=status,
        page=page,
        page_size=page_size,
    )

    total_pages = (total + page_size - 1) // page_size if total > 0 else 0

    paginated = PaginatedData(
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

    data = jsonable_encoder(paginated)
    return envelope(data, request_id=context.request_id, correlation_id=context.correlation_id)


# ═══════════════════════════════════════════════════════════════════
# GET /api/v4/inbox/{inbox_item_id} — Get inbox item detail
# ═══════════════════════════════════════════════════════════════════

@router.get("/{inbox_item_id}", response_model=ResponseEnvelope[InboxItemRead])
def get_inbox_route(
    inbox_item_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Get the details of a single inbox item."""
    item = get_inbox_item(db, inbox_item_id)
    if item is None:
        raise ApiError(404, "not_found", f"收件项 {inbox_item_id} 不存在")

    data = jsonable_encoder(item)
    return envelope(data, request_id=context.request_id, correlation_id=context.correlation_id)


# ═══════════════════════════════════════════════════════════════════
# POST /api/v4/inbox/{inbox_item_id}/process — Trigger processing
# ═══════════════════════════════════════════════════════════════════

@router.post("/{inbox_item_id}/process", response_model=ResponseEnvelope[InboxItemRead])
def process_inbox_route(
    inbox_item_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Trigger processing for an inbox item.

    Advances the inbox item status:
    - ``received`` -> ``staged`` (for non-file items that need preparation)
    - ``staged`` -> ``linked`` (if an asset has been created)
    - ``linked`` -> ``processed`` (mark as done)

    In Phase 3, this is a manual trigger.  In Phase 4+, the pipeline
    consumer will advance these states automatically.
    """
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "写请求必须提供 Idempotency-Key 头")

    _guard_idempotency(db, context.idempotency_key, "inbox_item")
    context = _resolve_actor(auth, context)

    item = get_inbox_item(db, inbox_item_id)
    if item is None:
        raise ApiError(404, "not_found", f"收件项 {inbox_item_id} 不存在")

    current_status = item.status.value if hasattr(item.status, 'value') else item.status

    # Determine the next status
    _NEXT_STATUS = {
        "received": "staged",
        "staged": "linked",   # requires asset_id to be set — typically done by pipeline
        "linked": "processed",
    }

    next_status = _NEXT_STATUS.get(current_status)
    if next_status is None:
        raise ApiError(
            400,
            "bad_request",
            f"收件项状态 '{current_status}' 无法继续处理（终态或已处理）",
        )

    # For 'staged -> linked', we'd need an asset_id.  In P3-02 this is
    # typically set by the asset creation flow.  If no asset_id exists
    # yet, the caller should use the asset creation endpoint instead.
    if current_status == "staged" and item.asset_id is None:
        raise ApiError(
            400,
            "bad_request",
            "收件项尚未关联资产。请先通过 POST /api/v4/assets 创建资产，"
            "再关联此收件项。",
            details={"inbox_item_id": str(inbox_item_id)},
        )

    updated = update_inbox_status(
        db,
        context,
        inbox_item_id=inbox_item_id,
        new_status=next_status,
        expected_status=current_status,
    )

    data = jsonable_encoder(updated)
    return envelope(data, request_id=context.request_id, correlation_id=context.correlation_id)
