"""Project API routes.

``POST /api/v4/projects`` is the **Phase 1 formal write path** that demonstrates
the audit + outbox + idempotency write loop:

1. Policy Engine check — only ``owner`` / ``operator`` may create projects.
2. Idempotency key — replay returns the previously created project.
3. Single transaction — ``projects``, ``audit_events``, ``events`` committed
   together (or rolled back together on failure).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
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
from mneme.db.idempotency import IdempotencyConflict, check_idempotency_key_any
from mneme.db.projects import (
    archive_project,
    create_project,
    get_project,
    get_project_by_code,
    list_projects,
    update_project,
)
from mneme.schemas import (
    PageInfo,
    ProjectCreateRequest,
    ProjectListResponse,
    ProjectUpdateRequest,
    ResponseEnvelope,
)
from mneme.security import (
    Action,
    Object,
    PolicyContext,
    actor_from_user_session,
    can,
)
from mneme.security.audit import audit_event_for_policy_denied, audit_event_for_action
from mneme.db.audit import add_audit_event


router = APIRouter(prefix="/projects", tags=["projects"])


@router.post("", response_model=ResponseEnvelope)
def create_project_route(
    payload: ProjectCreateRequest,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Create a project (Phase 1 formal write path).

    This endpoint is the canonical example of an idempotent write that records
    an audit event and publishes an outbox event in the same transaction as the
    business-row insert.
    """
    # ── Idempotency-key is required on write endpoints ─────────────────────
    if not context.idempotency_key:
        raise ApiError(
            400,
            "bad_request",
            "写请求必须提供 Idempotency-Key 头",
        )

    # ── Validate idempotency-key does not clash with a different aggregate ─
    _guard_idempotency_type(db, context.idempotency_key, "project")

    # ── Check for duplicate project_code ───────────────────────────────────
    existing = get_project_by_code(db, payload.project_code)
    if existing is not None:
        raise ApiError(
            409,
            "idempotency_conflict",
            f"项目代码 '{payload.project_code}' 已存在",
        )

    # ── Policy check ───────────────────────────────────────────────────────
    policy_actor = actor_from_user_session(
        user_id=auth.user.user_id,
        role=auth.user.role_code.value,
        status=auth.user.status.value,
    )
    policy_action = Action(name="project.create")
    policy_object = Object(object_type="project")
    policy_ctx = PolicyContext(
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )

    decision = can(policy_actor, policy_action, policy_object, policy_ctx)
    if decision.decision.value != "allow":
        audit = audit_event_for_policy_denied(
            action="project.create",
            decision=decision,
            object_type="project",
        )
        add_audit_event(db, context, audit)
        raise ApiError(
            403,
            "permission_denied",
            decision.message or "权限不足",
            details=decision.details,
        )

    # ── Wire authenticated actor into request context for object registry ──
    context = with_actor(
        context,
        actor=ActorContext(
            actor_type="user",
            actor_id=auth.user.user_id,
            auth_context_type="user_session",
            auth_context_id=auth.session.session_id,
        ),
    )

    # ── Business write with audit + outbox + idempotency + object registry ─
    project = create_project(db, context, payload=payload)

    return envelope(
        jsonable_encoder(project),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


def _guard_idempotency_type(
    db: Session,
    idempotency_key: str,
    expected_aggregate_type: str,
) -> None:
    """Reject a request whose idempotency key was already used for a
    different aggregate type (e.g. creating a project with a key that was
    previously used for an agent token).

    Without this guard a client could accidentally (or maliciously) replay an
    idempotency key across resource types and get back a confusing response.
    """
    existing = check_idempotency_key_any(db, idempotency_key=idempotency_key)
    if existing is None:
        return

    _event_id, actual_type, _aggregate_id = existing
    if actual_type != expected_aggregate_type:
        raise ApiError(
            409,
            "idempotency_conflict",
            f"幂等键已被用于 '{actual_type}'，而非 '{expected_aggregate_type}'",
            details={
                "expected_aggregate_type": expected_aggregate_type,
                "existing_aggregate_type": actual_type,
            },
        )


# ── List projects ───────────────────────────────────────────────────────────────


@router.get("", response_model=ResponseEnvelope)
def list_projects_route(
    page: int = 1,
    page_size: int = 50,
    status: str | None = None,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    _auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """List projects with optional status filter and pagination.

    Query params:
    - ``page``: page number (default 1)
    - ``page_size``: items per page (default 50)
    - ``status``: filter by status (active, archived, disabled). None = non-archived.
    """
    from math import ceil

    projects, total = list_projects(db, page=page, page_size=page_size, status=status)

    total_pages = max(1, ceil(total / max(page_size, 1)))
    page_info = PageInfo(
        page=page,
        page_size=page_size,
        total_items=total,
        total_pages=total_pages,
        has_next=page < total_pages,
        has_previous=page > 1,
    )

    data = ProjectListResponse(items=projects, page_info=page_info)
    return envelope(
        jsonable_encoder(data),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ── Get project ─────────────────────────────────────────────────────────────────


@router.get("/{project_id}", response_model=ResponseEnvelope)
def get_project_route(
    project_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    _auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Get a single project by ID."""
    project = get_project(db, project_id)
    if project is None:
        raise ApiError(404, "bad_request", f"项目 '{project_id}' 未找到")

    return envelope(
        jsonable_encoder(project),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ── Update project ──────────────────────────────────────────────────────────────


@router.put("/{project_id}", response_model=ResponseEnvelope)
def update_project_route(
    project_id: UUID,
    payload: ProjectUpdateRequest,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Update an existing project. Only provided fields are changed."""
    # Verify project exists
    existing = get_project(db, project_id)
    if existing is None:
        raise ApiError(404, "bad_request", f"项目 '{project_id}' 未找到")

    # Policy check
    policy_actor = actor_from_user_session(
        user_id=auth.user.user_id,
        role=auth.user.role_code.value,
        status=auth.user.status.value,
    )
    policy_action = Action(name="project.update")
    policy_object = Object(object_type="project", object_id=str(project_id))
    policy_ctx = PolicyContext(
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )

    decision = can(policy_actor, policy_action, policy_object, policy_ctx)
    if decision.decision.value != "allow":
        audit = audit_event_for_policy_denied(
            action="project.update",
            decision=decision,
            object_type="project",
            object_id=str(project_id),
        )
        add_audit_event(db, context, audit)
        raise ApiError(
            403,
            "permission_denied",
            decision.message or "权限不足",
            details=decision.details,
        )

    # Wire actor context
    context = with_actor(
        context,
        actor=ActorContext(
            actor_type="user",
            actor_id=auth.user.user_id,
            auth_context_type="user_session",
            auth_context_id=auth.session.session_id,
        ),
    )

    updated = update_project(
        db,
        project_id=project_id,
        name=payload.name,
        description=payload.description,
        description_set=payload.description is not None,
        sensitivity_default=payload.sensitivity_default.value if payload.sensitivity_default else None,
    )

    if updated is None:
        raise ApiError(404, "bad_request", f"项目 '{project_id}' 未找到或已归档")

    # Audit
    add_audit_event(
        db,
        context,
        audit_event_for_action(
            action="project.updated",
            result="success",
            object_type="project",
            object_id=project_id,
            diff_summary={
                k: v for k, v in payload.model_dump(exclude_none=True).items()
            },
        ),
    )
    db.commit()

    return envelope(
        jsonable_encoder(updated),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ── Archive project ─────────────────────────────────────────────────────────────


@router.delete("/{project_id}", response_model=ResponseEnvelope)
def archive_project_route(
    project_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Archive (soft-delete) a project."""
    # Verify project exists
    existing = get_project(db, project_id)
    if existing is None:
        raise ApiError(404, "bad_request", f"项目 '{project_id}' 未找到")

    # Policy check
    policy_actor = actor_from_user_session(
        user_id=auth.user.user_id,
        role=auth.user.role_code.value,
        status=auth.user.status.value,
    )
    policy_action = Action(name="project.delete")
    policy_object = Object(object_type="project", object_id=str(project_id))
    policy_ctx = PolicyContext(
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )

    decision = can(policy_actor, policy_action, policy_object, policy_ctx)
    if decision.decision.value != "allow":
        audit = audit_event_for_policy_denied(
            action="project.delete",
            decision=decision,
            object_type="project",
            object_id=str(project_id),
        )
        add_audit_event(db, context, audit)
        raise ApiError(
            403,
            "permission_denied",
            decision.message or "权限不足",
            details=decision.details,
        )

    # Wire actor context
    context = with_actor(
        context,
        actor=ActorContext(
            actor_type="user",
            actor_id=auth.user.user_id,
            auth_context_type="user_session",
            auth_context_id=auth.session.session_id,
        ),
    )

    archived = archive_project(db, project_id=project_id)

    if archived is None:
        raise ApiError(404, "bad_request", f"项目 '{project_id}' 未找到或已归档")

    # Audit
    add_audit_event(
        db,
        context,
        audit_event_for_action(
            action="project.archived",
            result="success",
            object_type="project",
            object_id=project_id,
        ),
    )
    db.commit()

    return envelope(
        jsonable_encoder(archived),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )
