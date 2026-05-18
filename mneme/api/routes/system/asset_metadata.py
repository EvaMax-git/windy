"""Asset Metadata API routes.

Provides standalone CRUD endpoints for asset metadata:

* ``POST /api/v4/asset-metadata`` — Create metadata entry
* ``GET /api/v4/asset-metadata`` — List metadata (filter by asset, paginate)
* ``GET /api/v4/asset-metadata/{metadata_id}`` — Get single metadata entry
* ``PATCH /api/v4/asset-metadata/{metadata_id}`` — Update metadata entry
* ``DELETE /api/v4/asset-metadata/{metadata_id}`` — Delete metadata entry
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
from mneme.db.asset_metadata import (
    add_metadata,
    delete_metadata,
    get_metadata_by_id,
    list_metadata,
    update_metadata,
)
from mneme.db.assets import get_asset
from mneme.db.audit import add_audit_event as db_add_audit_event
from mneme.schemas import (
    AssetMetadataCreateRequest,
    AssetMetadataRead,
    AssetMetadataUpdateRequest,
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


router = APIRouter(prefix="/asset-metadata", tags=["asset-metadata"])


# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════


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
    policy_object = Object(object_type="asset_metadata")
    policy_ctx = PolicyContext(
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )

    decision = can(policy_actor, policy_action, policy_object, policy_ctx)
    if decision.decision.value != "allow":
        audit = audit_event_for_policy_denied(
            action=action_name,
            decision=decision,
            object_type="asset_metadata",
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


def _verify_asset_exists(db: Session, asset_id: UUID) -> None:
    """Verify that the referenced asset exists, raising 404 if not."""
    asset = get_asset(db, asset_id)
    if asset is None:
        raise ApiError(404, "not_found", f"Asset {asset_id} not found")


# ═══════════════════════════════════════════════════════════════════
# Routes
# ═══════════════════════════════════════════════════════════════════


@router.post("", response_model=ResponseEnvelope[AssetMetadataRead])
def create_asset_metadata_route(
    payload: AssetMetadataCreateRequest,
    asset_id: UUID = Query(..., description="The asset to attach metadata to"),
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Create a metadata entry for an asset.

    Uses upsert semantics: if a metadata entry with the same
    (asset_id, metadata_key, source) already exists, it is updated.
    """
    if not context.idempotency_key:
        raise ApiError(
            400, "bad_request", "Idempotency-Key header is required for writes"
        )

    _policy_check("asset.update", auth, db, context)
    context = _wire_actor(context, auth)

    _verify_asset_exists(db, asset_id)

    try:
        meta = add_metadata(db, context, asset_id=asset_id, payload=payload)
    except ValueError as exc:
        raise ApiError(400, "bad_request", str(exc)) from exc

    return envelope(
        jsonable_encoder(meta),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.get("", response_model=ResponseEnvelope[PaginatedData[AssetMetadataRead]])
def list_asset_metadata_route(
    asset_id: UUID | None = Query(None, description="Filter by asset ID"),
    metadata_key: str | None = Query(None, description="Filter by metadata key"),
    source: str | None = Query(None, description="Filter by source namespace"),
    page: int = Query(1, ge=1, description="Page number (1-based)"),
    page_size: int = Query(50, ge=1, le=200, description="Items per page"),
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """List metadata entries with optional filters and pagination.

    When ``asset_id`` is provided, only metadata for that asset is returned.
    Additional filters on ``metadata_key`` and ``source`` are applied
    client-side after fetching.
    """
    _policy_check("asset.read", auth, db, context)

    if asset_id is None:
        raise ApiError(
            400, "bad_request", "Query parameter 'asset_id' is required"
        )

    _verify_asset_exists(db, asset_id)

    all_items = list_metadata(db, asset_id=asset_id)

    # Client-side filtering by key and source
    if metadata_key:
        all_items = [
            item for item in all_items
            if item.metadata_key == metadata_key
        ]
    if source:
        all_items = [
            item for item in all_items
            if item.source == source
        ]

    total = len(all_items)
    start = (page - 1) * page_size
    paged_items = all_items[start : start + page_size]

    result = _build_paginated_response(paged_items, total, page, page_size)
    return envelope(
        jsonable_encoder(result),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.get("/{metadata_id}", response_model=ResponseEnvelope[AssetMetadataRead])
def get_asset_metadata_route(
    metadata_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Get a single metadata entry by ID."""
    _policy_check("asset.read", auth, db, context)

    meta = get_metadata_by_id(db, asset_metadata_id=metadata_id)
    if meta is None:
        raise ApiError(
            404, "not_found", f"Asset metadata {metadata_id} not found"
        )

    return envelope(
        jsonable_encoder(meta),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.patch("/{metadata_id}", response_model=ResponseEnvelope[AssetMetadataRead])
def update_asset_metadata_route(
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

    # Resolve the entry to determine its asset_id
    existing = get_metadata_by_id(db, asset_metadata_id=metadata_id)
    if existing is None:
        raise ApiError(
            404, "not_found", f"Asset metadata {metadata_id} not found"
        )

    try:
        meta = update_metadata(
            db,
            context,
            asset_metadata_id=metadata_id,
            asset_id=existing.asset_id,
            payload=payload,
        )
    except ValueError as exc:
        raise ApiError(400, "bad_request", str(exc)) from exc

    return envelope(
        jsonable_encoder(meta),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.delete("/{metadata_id}", response_model=ResponseEnvelope[AssetMetadataRead])
def delete_asset_metadata_route(
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

    # Resolve the entry to determine its asset_id
    existing = get_metadata_by_id(db, asset_metadata_id=metadata_id)
    if existing is None:
        raise ApiError(
            404, "not_found", f"Asset metadata {metadata_id} not found"
        )

    try:
        meta = delete_metadata(
            db,
            context,
            asset_metadata_id=metadata_id,
            asset_id=existing.asset_id,
        )
    except ValueError as exc:
        raise ApiError(400, "bad_request", str(exc)) from exc

    return envelope(
        jsonable_encoder(meta),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )
