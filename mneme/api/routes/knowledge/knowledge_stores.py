from __future__ import annotations

from math import ceil
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
from mneme.db.audit import AuditEvent, add_audit_event
from mneme.db.auth import AuthenticatedSession
from mneme.db.base import get_db
from mneme.db.sub_library_registry import (
    create_sub_library,
    delete_sub_library,
    get_sub_library,
    list_sub_libraries,
    update_sub_library,
)
from mneme.schemas.common import ResponseEnvelope
from mneme.schemas.sub_libraries import (
    SubLibraryCreateRequest,
    SubLibraryListResponse,
    SubLibraryRead,
    SubLibraryUpdateRequest,
)
from mneme.security import (
    Action,
    Object,
    PolicyContext,
    actor_from_user_session,
    can,
)
from mneme.security.audit import audit_event_for_policy_denied, audit_event_for_action

router = APIRouter(prefix="/sub-libraries", tags=["sub_libraries"])


# ── Policy helpers ─────────────────────────────────────────────────────────────

def _policy_actor_from_auth(auth: AuthenticatedSession):
    """Build a policy Actor from the authenticated user session."""
    return actor_from_user_session(
        user_id=auth.user.user_id,
        role=auth.user.role_code.value,
        status=auth.user.status.value,
    )


def _require_idempotency(context: RequestContext) -> None:
    """Raise 400 if Idempotency-Key header is missing."""
    if not context.idempotency_key:
        raise ApiError(
            400, "bad_request", "Idempotency-Key header is required for writes"
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


def _policy_check(
    db: Session,
    auth: AuthenticatedSession,
    context: RequestContext,
    action_name: str,
    object_type: str = "knowledge_store",
    object_id: str | None = None,
) -> None:
    """Run policy check and raise 403 if denied."""
    policy_actor = _policy_actor_from_auth(auth)
    policy_action = Action(name=action_name)
    policy_object = Object(object_type=object_type, object_id=object_id)
    policy_ctx = PolicyContext(
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )

    decision = can(policy_actor, policy_action, policy_object, policy_ctx)
    if decision.decision.value != "allow":
        audit = audit_event_for_policy_denied(
            action=action_name,
            decision=decision,
            object_type=object_type,
            object_id=object_id,
        )
        add_audit_event(db, context, audit)
        raise ApiError(
            403,
            "permission_denied",
            decision.message or "权限不足",
            details=decision.details,
        )


# ── Routes ─────────────────────────────────────────────────────────────────────


@router.post("", response_model=ResponseEnvelope[SubLibraryRead], status_code=201)
def create_sub_library_route(
    payload: SubLibraryCreateRequest,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Register a new knowledge store backend.

    Requires ``Idempotency-Key`` header for safe retry.
    """
    _require_idempotency(context)
    _policy_check(db, auth, context, "knowledge_store.create")

    context = _wire_actor(context, auth)

    try:
        library = create_sub_library(db, payload=payload)
    except ValueError as exc:
        _write_audit(db, context, "knowledge_store.create", "failure",
                      object_id=None, reason=str(exc))
        raise ApiError(400, "bad_request", str(exc)) from exc

    _write_audit(db, context, "knowledge_store.create", "success",
                 object_id=library.id if hasattr(library, 'id') else None)

    db.commit()

    return envelope(
        jsonable_encoder(library),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.get("", response_model=ResponseEnvelope[SubLibraryListResponse])
def list_sub_libraries_route(
    type: str | None = Query(default=None, description="Filter by knowledge store type (vector|graph|fulltext|custom)"),
    page: int = Query(default=1, ge=1, description="Page number"),
    page_size: int = Query(default=50, ge=1, le=200, description="Items per page"),
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """List registered knowledge stores with optional type filter and pagination."""
    _policy_check(db, auth, context, "knowledge_store.list")
    context = _wire_actor(context, auth)

    result = list_sub_libraries(db, type_filter=type, page=page, page_size=page_size)

    return envelope(
        jsonable_encoder(result),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.get("/{lib_id}", response_model=ResponseEnvelope[SubLibraryRead])
def get_sub_library_route(
    lib_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Look up a single knowledge store by ID."""
    _policy_check(db, auth, context, "knowledge_store.get", object_id=str(lib_id))
    context = _wire_actor(context, auth)

    library = get_sub_library(db, lib_id)
    if library is None:
        raise ApiError(404, "bad_request", f"知识库未找到: {lib_id}")

    return envelope(
        jsonable_encoder(library),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.put("/{lib_id}", response_model=ResponseEnvelope[SubLibraryRead])
def update_sub_library_route(
    lib_id: UUID,
    payload: SubLibraryUpdateRequest,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Update an existing knowledge store registration.

    Requires ``Idempotency-Key`` header for safe retry.
    """
    _require_idempotency(context)
    _policy_check(db, auth, context, "knowledge_store.update", object_id=str(lib_id))

    # Verify it exists
    existing = get_sub_library(db, lib_id)
    if existing is None:
        raise ApiError(404, "bad_request", f"知识库未找到: {lib_id}")

    context = _wire_actor(context, auth)

    updated = update_sub_library(
        db,
        lib_id,
        name=payload.name,
        type=payload.type,
        key=payload.key,
        capability_json=payload.capability_json,
        metadata_json=payload.metadata_json,
    )

    if updated is None:
        raise ApiError(404, "bad_request", f"知识库未找到: {lib_id}")

    add_audit_event(
        db,
        context,
        audit_event_for_action(
            action="knowledge_store.updated",
            result="success",
            object_type="knowledge_store",
            object_id=lib_id,
            diff_summary=payload.model_dump(exclude_none=True, mode="json"),
        ),
    )

    db.commit()

    return envelope(
        jsonable_encoder(updated),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.delete("/{lib_id}", response_model=ResponseEnvelope[dict])
def delete_sub_library_route(
    lib_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Remove a knowledge store registration.

    Requires ``Idempotency-Key`` header for safe retry.
    """
    _require_idempotency(context)
    _policy_check(db, auth, context, "knowledge_store.delete", object_id=str(lib_id))

    context = _wire_actor(context, auth)

    deleted = delete_sub_library(db, lib_id=lib_id)
    if not deleted:
        _write_audit(db, context, "knowledge_store.delete", "failure",
                      object_id=lib_id, reason="not_found")
        raise ApiError(404, "bad_request", f"知识库未找到: {lib_id}")

    _write_audit(db, context, "knowledge_store.delete", "success",
                 object_id=lib_id)

    db.commit()

    return envelope(
        jsonable_encoder({"deleted": True, "id": str(lib_id)}),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ── Audit helper ───────────────────────────────────────────────────────────────

def _write_audit(
    db: Session,
    context: RequestContext,
    action: str,
    result: str,
    *,
    object_id: UUID | None = None,
    reason: str | None = None,
) -> None:
    """Write an audit event record inline (non-fatal if audit write fails)."""
    try:
        add_audit_event(
            db,
            context,
            AuditEvent(
                action=action,
                result=result,
                object_type="knowledge_store",
                object_id=object_id,
                reason_code=reason,
            ),
        )
    except Exception:
        pass  # Audit write failure must not break the response
