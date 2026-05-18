"""Asset API routes.

Provides CRUD endpoints for assets and asset metadata:

* ``POST /api/v4/assets`` — Create asset
* ``GET /api/v4/assets`` — List assets (filter, paginate)
* ``GET /api/v4/assets/{asset_id}`` — Asset detail
* ``PATCH /api/v4/assets/{asset_id}`` — Update asset
* ``DELETE /api/v4/assets/{asset_id}`` — Soft-delete asset
* ``POST /api/v4/assets/{asset_id}/metadata`` — Add/update metadata
* ``GET /api/v4/assets/{asset_id}/metadata`` — List metadata
"""

from __future__ import annotations

import math
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
from mneme.db.assets import (
    add_metadata,
    advance_ingest_state,
    archive_asset,
    change_asset_status,
    create_asset,
    delete_metadata,
    DuplicateAssetError,
    get_asset,
    get_asset_by_uid,
    get_metadata_by_id,
    ingest_asset,
    list_assets,
    list_metadata,
    lookup_asset_by_hash,
    promote_from_staging,
    restore_asset,
    update_asset,
    update_metadata,
)
from mneme.db.projects import get_project
from mneme.schemas import (
    AssetCreateRequest,
    AssetMetadataCreateRequest,
    AssetMetadataRead,
    AssetMetadataUpdateRequest,
    AssetRead,
    AssetUpdateRequest,
    PageInfo,
    PaginatedData,
    ResponseEnvelope,
)
from mneme.security import (
    Action,
    Object,
    PolicyContext,
    actor_from_user_session,
    can,
)
from mneme.security.audit import audit_event_for_policy_denied
from mneme.db.audit import add_audit_event as db_add_audit_event
from mneme.storage.promote import PromoteError
from mneme.storage.upload import (
    handle_idempotent_upload_stream,
    validate_filename,
)


router = APIRouter(prefix="/assets", tags=["assets"])


# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════

_SENSITIVITY_ORDER = ["public", "normal", "private", "sensitive", "secret"]


def _can_view_sensitivity(user_sens: str, target_sens: str) -> bool:
    """Check if a user with *user_sens* can view *target_sens*."""
    return _SENSITIVITY_ORDER.index(user_sens) >= _SENSITIVITY_ORDER.index(target_sens)


def _build_paginated_response(
    items: list,
    total: int,
    page: int,
    page_size: int,
) -> PaginatedData:
    """Build a PaginatedData response."""
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


def _policy_check(
    action_name: str,
    auth: AuthenticatedSession,
    db: Session,
    context: RequestContext,
) -> None:
    """Perform policy check and raise ApiError(403) if denied."""
    policy_actor = actor_from_user_session(
        user_id=auth.user.user_id,
        role=auth.user.role_code.value,
        status=auth.user.status.value,
    )
    policy_action = Action(name=action_name)
    policy_object = Object(object_type="asset")
    policy_ctx = PolicyContext(
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )

    decision = can(policy_actor, policy_action, policy_object, policy_ctx)
    if decision.decision.value != "allow":
        audit = audit_event_for_policy_denied(
            action=action_name,
            decision=decision,
            object_type="asset",
        )
        db_add_audit_event(db, context, audit)
        raise ApiError(
            403,
            "permission_denied",
            decision.message or "Permission denied",
            details=decision.details,
        )


def _wire_actor(
    context: RequestContext, auth: AuthenticatedSession
) -> RequestContext:
    """Wire the authenticated user into the request context."""
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
# Routes
# ═══════════════════════════════════════════════════════════════════


@router.post("", response_model=ResponseEnvelope[AssetRead], status_code=201)
def create_asset_route(
    payload: AssetCreateRequest,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Create a new asset record.

    The asset is created in ``ingest_state='pending'`` with
    ``storage_ref='pending'``.  File promotion to permanent storage
    happens via ``promote_from_staging`` (typically called by the
    inbox→asset pipeline).
    """
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "Idempotency-Key header is required for writes")

    _policy_check("asset.create", auth, db, context)
    context = _wire_actor(context, auth)

    try:
        asset = create_asset(db, context, payload=payload)
    except ValueError as exc:
        raise ApiError(400, "bad_request", str(exc)) from exc

    return envelope(
        jsonable_encoder(asset),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.get("", response_model=ResponseEnvelope[PaginatedData[AssetRead]])
def list_assets_route(
    project_id: UUID | None = Query(None, description="Filter by project"),
    asset_type: str | None = Query(None, description="Filter by asset type"),
    knowledge_state: str | None = Query(None, description="Filter by knowledge state"),
    sensitivity_level: str | None = Query(None, description="Filter by sensitivity level"),
    status: str | None = Query(None, description="Filter by status"),
    ingest_state: str | None = Query(None, description="Filter by ingest state"),
    page: int = Query(1, ge=1, description="Page number (1-based)"),
    page_size: int = Query(50, ge=1, le=200, description="Items per page"),
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """List assets with optional filters and pagination."""
    _policy_check("asset.list", auth, db, context)

    items, total = list_assets(
        db,
        project_id=project_id,
        asset_type=asset_type,
        knowledge_state=knowledge_state,
        sensitivity_level=sensitivity_level,
        status=status,
        ingest_state=ingest_state,
        page=page,
        page_size=page_size,
    )

    result = _build_paginated_response(items, total, page, page_size)
    return envelope(
        jsonable_encoder(result),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.get("/{asset_id}", response_model=ResponseEnvelope[AssetRead])
def get_asset_route(
    asset_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Get asset details by ID."""
    _policy_check("asset.read", auth, db, context)

    asset = get_asset(db, asset_id)
    if asset is None:
        raise ApiError(404, "bad_request", f"Asset {asset_id} not found")

    return envelope(
        jsonable_encoder(asset),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.patch("/{asset_id}", response_model=ResponseEnvelope[AssetRead])
def update_asset_route(
    asset_id: UUID,
    payload: AssetUpdateRequest,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Update mutable fields of an asset.

    Only non-None fields in the payload are applied.
    Sensitivity level can only be raised, not lowered.
    """
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "Idempotency-Key header is required for writes")

    _policy_check("asset.update", auth, db, context)
    context = _wire_actor(context, auth)

    sensitivity_str = (
        payload.sensitivity_level.value
        if payload.sensitivity_level and hasattr(payload.sensitivity_level, "value")
        else payload.sensitivity_level
    )
    retention_str = (
        payload.retention_policy.value
        if payload.retention_policy and hasattr(payload.retention_policy, "value")
        else payload.retention_policy
    )

    try:
        asset = update_asset(
            db,
            context,
            asset_id=asset_id,
            title=payload.title,
            sensitivity_level=sensitivity_str,
            retention_policy=retention_str,
        )
    except ValueError as exc:
        raise ApiError(400, "bad_request", str(exc)) from exc

    return envelope(
        jsonable_encoder(asset),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.delete("/{asset_id}", response_model=ResponseEnvelope[AssetRead])
def delete_asset_route(
    asset_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Soft-delete an asset (status='deleted').

    Does NOT delete the physical file on disk.
    """
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "Idempotency-Key header is required for writes")

    _policy_check("asset.delete", auth, db, context)
    context = _wire_actor(context, auth)

    try:
        asset = archive_asset(db, context, asset_id=asset_id, new_status="deleted")
    except ValueError as exc:
        raise ApiError(400, "bad_request", str(exc)) from exc

    return envelope(
        jsonable_encoder(asset),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.post("/{asset_id}/restore", response_model=ResponseEnvelope[AssetRead])
def restore_asset_route(
    asset_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Restore a soft-deleted, archived, or quarantined asset to ``'active'``.

    Clears ``archived_at`` on success.  Does NOT re-create physical files.
    """
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "Idempotency-Key header is required for writes")

    _policy_check("asset.update", auth, db, context)
    context = _wire_actor(context, auth)

    try:
        asset = restore_asset(db, context, asset_id=asset_id)
    except ValueError as exc:
        raise ApiError(400, "bad_request", str(exc)) from exc

    return envelope(
        jsonable_encoder(asset),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.patch("/{asset_id}/status", response_model=ResponseEnvelope[AssetRead])
def change_asset_status_route(
    asset_id: UUID,
    new_status: str = Query(..., description="Target status: archived, deleted, quarantined, or active"),
    expected_status: str | None = Query(None, description="Expected current status for optimistic concurrency"),
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Atomically transition an asset's ``status`` with state-machine validation.

    Valid transitions:

    * ``active → archived``
    * ``active → deleted``
    * ``active → quarantined``
    * ``archived → active``
    * ``deleted → active``
    * ``quarantined → active``

    Use ``expected_status`` for optimistic concurrency control.
    """
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "Idempotency-Key header is required for writes")

    _policy_check("asset.update", auth, db, context)
    context = _wire_actor(context, auth)

    try:
        asset = change_asset_status(
            db, context,
            asset_id=asset_id,
            new_status=new_status,
            expected_status=expected_status,
        )
    except ValueError as exc:
        raise ApiError(400, "bad_request", str(exc)) from exc

    return envelope(
        jsonable_encoder(asset),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.post(
    "/{asset_id}/metadata",
    response_model=ResponseEnvelope[AssetMetadataRead],
)
def add_asset_metadata_route(
    asset_id: UUID,
    payload: AssetMetadataCreateRequest,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Add or update a metadata entry for an asset.

    Uses upsert semantics: if a metadata entry with the same
    (asset_id, metadata_key, source) already exists, it is updated.
    """
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "Idempotency-Key header is required for writes")

    _policy_check("asset.update", auth, db, context)
    context = _wire_actor(context, auth)

    try:
        meta = add_metadata(db, context, asset_id=asset_id, payload=payload)
    except ValueError as exc:
        raise ApiError(400, "bad_request", str(exc)) from exc

    return envelope(
        jsonable_encoder(meta),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.get(
    "/{asset_id}/metadata",
    response_model=ResponseEnvelope[list[AssetMetadataRead]],
)
def list_asset_metadata_route(
    asset_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """List all metadata entries for an asset."""
    _policy_check("asset.read", auth, db, context)

    # Verify asset exists
    asset = get_asset(db, asset_id)
    if asset is None:
        raise ApiError(404, "bad_request", f"Asset {asset_id} not found")

    items = list_metadata(db, asset_id=asset_id)
    return envelope(
        jsonable_encoder(items),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.get(
    "/{asset_id}/metadata/{metadata_id}",
    response_model=ResponseEnvelope[AssetMetadataRead],
)
def get_asset_metadata_route(
    asset_id: UUID,
    metadata_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Get a single metadata entry by ID."""
    _policy_check("asset.read", auth, db, context)

    # Verify asset exists
    asset = get_asset(db, asset_id)
    if asset is None:
        raise ApiError(404, "bad_request", f"Asset {asset_id} not found")

    meta = get_metadata_by_id(db, asset_metadata_id=metadata_id)
    if meta is None:
        raise ApiError(
            404, "bad_request", f"Metadata {metadata_id} not found"
        )

    if meta.asset_id != asset_id:
        raise ApiError(
            404, "bad_request",
            f"Metadata {metadata_id} does not belong to asset {asset_id}",
        )

    return envelope(
        jsonable_encoder(meta),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.patch(
    "/{asset_id}/metadata/{metadata_id}",
    response_model=ResponseEnvelope[AssetMetadataRead],
)
def update_asset_metadata_route(
    asset_id: UUID,
    metadata_id: UUID,
    payload: AssetMetadataUpdateRequest,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Partially update a metadata entry.

    Only non-None fields in the payload are applied.  If ``value_type``
    is changed, the existing ``metadata_value`` is re-validated against
    the new type.
    """
    if not context.idempotency_key:
        raise ApiError(
            400, "bad_request", "Idempotency-Key header is required for writes"
        )

    _policy_check("asset.update", auth, db, context)
    context = _wire_actor(context, auth)

    try:
        meta = update_metadata(
            db,
            context,
            asset_metadata_id=metadata_id,
            asset_id=asset_id,
            payload=payload,
        )
    except ValueError as exc:
        raise ApiError(400, "bad_request", str(exc)) from exc

    return envelope(
        jsonable_encoder(meta),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.delete(
    "/{asset_id}/metadata/{metadata_id}",
    response_model=ResponseEnvelope[AssetMetadataRead],
)
def delete_asset_metadata_route(
    asset_id: UUID,
    metadata_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Delete a metadata entry.

    Returns the deleted row.  The ``assets.metadata_json`` cache is
    rebuilt automatically.
    """
    if not context.idempotency_key:
        raise ApiError(
            400, "bad_request", "Idempotency-Key header is required for writes"
        )

    _policy_check("asset.update", auth, db, context)
    context = _wire_actor(context, auth)

    try:
        meta = delete_metadata(
            db,
            context,
            asset_metadata_id=metadata_id,
            asset_id=asset_id,
        )
    except ValueError as exc:
        raise ApiError(400, "bad_request", str(exc)) from exc

    return envelope(
        jsonable_encoder(meta),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════
# POST /api/v4/assets/ingest — Full upload → asset flow
# ═══════════════════════════════════════════════════════════════════


@router.post("/ingest", response_model=ResponseEnvelope[AssetRead], status_code=201)
async def ingest_asset_route(
    file: UploadFile = File(...),
    project_id: UUID | None = Form(None),
    title: str | None = Form(None),
    asset_type: str = Form("document"),
    sensitivity_level: str = Form("normal"),
    retention_policy: str = Form("default"),
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Upload a file and create a fully ingested Asset in one call.

    This is the primary Asset 入库 endpoint.  It performs the complete
    pipeline in a single request:

    1. Validate the uploaded file (name, size, MIME type).
    2. Stage the file to disk and compute its content hash.
    3. Check for duplicate assets by content hash (idempotent ingest).
       If a duplicate is found, the existing asset is returned.
    4. Create an inbox item (status='staged').
    5. Create the asset record with auto-generated ``asset_uid``.
    6. Promote the file from staging to permanent storage
       (``mneme_data/assets/{project_id}/{asset_uid}/``).
    7. Link the inbox item to the asset (``staged → linked``).

    Requires ``Idempotency-Key`` header for safe retry.
    """
    from fastapi import UploadFile

    if not context.idempotency_key:
        raise ApiError(
            400, "bad_request", "Idempotency-Key header is required for writes"
        )

    # Validate filename
    if not file.filename:
        raise ApiError(400, "bad_request", "Filename must not be empty")

    try:
        validate_filename(file.filename)
    except ValueError as e:
        raise ApiError(400, "bad_request", str(e))

    # Resolve project
    project = get_project(db, project_id) if project_id else None

    # Policy check
    _policy_check("asset.create", auth, db, context)
    context = _wire_actor(context, auth)

    # ── Stage the file via the storage layer ──────────────────────────
    # Use asset-level dedup (not inbox-level) since we're creating assets
    class _DuplicateFound(Exception):
        def __init__(self, existing_asset: AssetRead):
            self.existing_asset = existing_asset

    def lookup_dup(content_hash: str, pid: UUID | None) -> None:
        existing = lookup_asset_by_hash(
            db, content_hash=content_hash, project_id=pid
        )
        if existing is not None:
            raise _DuplicateFound(existing)

    try:
        upload_result = handle_idempotent_upload_stream(
            stream=file.file,
            original_filename=file.filename,
            project_id=project_id,
            lookup_duplicate=lookup_dup,
        )
    except _DuplicateFound as dup:
        # Idempotent ingest — return existing asset
        data = jsonable_encoder(dup.existing_asset)
        return envelope(
            data,
            request_id=context.request_id,
            correlation_id=context.correlation_id,
            meta={"duplicate": True, "asset_uid": dup.existing_asset.asset_uid},
        )

    if upload_result.staged_info is None:
        raise ApiError(500, "internal_error", "File staging failed: staged_info is empty")

    # ── Full ingest: inbox + asset + promote + link ───────────────────
    try:
        asset = ingest_asset(
            db,
            context,
            staged_info=upload_result.staged_info,
            project_id=project_id,
            project_code=project.project_code if project else "default",
            title=title or file.filename,
            asset_type=asset_type,
            sensitivity_level=sensitivity_level,
            retention_policy=retention_policy,
            source="api",
            source_uri=f"file://{upload_result.staged_info.staging_path}",
        )
    except DuplicateAssetError as dup:
        # Should not happen (caught above), but handle defensively
        data = jsonable_encoder(dup.existing_asset)
        return envelope(
            data,
            request_id=context.request_id,
            correlation_id=context.correlation_id,
            meta={"duplicate": True, "asset_uid": dup.existing_asset.asset_uid},
        )
    except (ValueError, PromoteError) as exc:
        raise ApiError(400, "bad_request", str(exc)) from exc
    except Exception as exc:
        raise ApiError(500, "internal_error", f"Asset ingest failed: {exc}") from exc

    data = jsonable_encoder(asset)
    return envelope(
        data,
        request_id=context.request_id,
        correlation_id=context.correlation_id,
        meta={"asset_uid": asset.asset_uid},
    )
