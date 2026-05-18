"""Pipeline API routes.

P3-04 Pipeline 骨架 — pipeline_defs + pipeline_runs CRUD endpoints:

* ``POST /api/v4/pipelines/defs`` — Create pipeline definition
* ``GET /api/v4/pipelines/defs`` — List pipeline definitions
* ``POST /api/v4/pipelines/runs`` — Manually trigger a pipeline run
* ``GET /api/v4/pipelines/runs`` — List pipeline runs
* ``GET /api/v4/pipelines/runs/{run_id}`` — Pipeline run detail with associated job
"""

from __future__ import annotations

import json
import math
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Query
from fastapi.encoders import jsonable_encoder
from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
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
from mneme.db.pipelines import (
    advance_run_status,
    create_pipeline_def,
    create_pipeline_run,
    get_pipeline_def,
    get_pipeline_run,
    list_pipeline_defs,
    list_pipeline_runs,
)
from mneme.db.jobs import get_job_by_id
from mneme.schemas import (
    PageInfo,
    PaginatedData,
    ResponseEnvelope,
)
from mneme.schemas.pipelines import (
    PipelineDefCreateRequest,
    PipelineDefRead,
    PipelineDefUpdateRequest,
    PipelineRunCreateRequest,
    PipelineRunDetail,
    PipelineRunRead,
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


router = APIRouter(prefix="/pipelines", tags=["pipelines"])


# ═══════════════════════════════════════════════════════════════════
# SQL — update pipeline_def
# ═══════════════════════════════════════════════════════════════════

_UPDATE_PIPELINE_DEF = text("""
    UPDATE pipeline_defs
    SET status = COALESCE(:status, status),
        name = COALESCE(:name, name),
        description = COALESCE(:description, description),
        config_json = COALESCE(:config_json, config_json),
        updated_at = now()
    WHERE pipeline_def_id = :pipeline_def_id
    RETURNING
        pipeline_def_id, project_id, pipeline_code, pipeline_type,
        version, name, description, config_json, status,
        created_by_user_id, created_at, updated_at
""").bindparams(bindparam("pipeline_def_id", type_=PG_UUID(as_uuid=True)))


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
    policy_object = Object(object_type="pipeline")
    policy_ctx = PolicyContext(
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )

    decision = can(policy_actor, policy_action, policy_object, policy_ctx)
    if decision.decision.value != "allow":
        audit = audit_event_for_policy_denied(
            action=action_name,
            decision=decision,
            object_type="pipeline",
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


def _require_idempotency(context: RequestContext) -> None:
    """Raise 400 if Idempotency-Key header is missing."""
    if not context.idempotency_key:
        raise ApiError(
            400, "bad_request", "Idempotency-Key header is required for writes"
        )


# ═══════════════════════════════════════════════════════════════════
# Routes — Pipeline Defs
# ═══════════════════════════════════════════════════════════════════


@router.post("/defs", response_model=ResponseEnvelope[PipelineDefRead])
def create_pipeline_def_route(
    payload: PipelineDefCreateRequest,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Create a pipeline definition.

    Requires ``Idempotency-Key`` header for safe retry.
    If ``config_json`` is omitted, defaults are used based on ``pipeline_type``.
    """
    if not context.idempotency_key:
        raise ApiError(
            400, "bad_request", "Idempotency-Key header is required for writes"
        )

    _policy_check("pipeline.create", auth, db, context)
    context = _wire_actor(context, auth)

    pipeline_type_str = (
        payload.pipeline_type.value
        if hasattr(payload.pipeline_type, "value")
        else payload.pipeline_type
    )
    status_str = (
        payload.status.value
        if hasattr(payload.status, "value")
        else payload.status
    )

    try:
        pipeline_def = create_pipeline_def(
            db,
            context,
            pipeline_code=payload.pipeline_code,
            pipeline_type=pipeline_type_str,
            name=payload.name,
            description=payload.description,
            config_json=payload.config_json,
            project_id=payload.project_id,
            status=status_str,
        )
    except ValueError as exc:
        raise ApiError(400, "bad_request", str(exc)) from exc

    return envelope(
        jsonable_encoder(pipeline_def),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.get("/defs", response_model=ResponseEnvelope[PaginatedData[PipelineDefRead]])
def list_pipeline_defs_route(
    pipeline_type: str | None = Query(None, description="Filter by pipeline type"),
    status: str | None = Query(None, description="Filter by status"),
    project_id: UUID | None = Query(None, description="Filter by project"),
    page: int = Query(1, ge=1, description="Page number (1-based)"),
    page_size: int = Query(50, ge=1, le=200, description="Items per page"),
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """List pipeline definitions with optional filters and pagination."""
    _policy_check("pipeline.read", auth, db, context)

    items, total = list_pipeline_defs(
        db,
        pipeline_type=pipeline_type,
        status=status,
        project_id=project_id,
        page=page,
        page_size=page_size,
    )

    result = _build_paginated_response(items, total, page, page_size)
    return envelope(
        jsonable_encoder(result),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.get("/defs/{pipeline_def_id}", response_model=ResponseEnvelope[PipelineDefRead])
def get_pipeline_def_route(
    pipeline_def_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Get a single pipeline definition by ID."""
    _policy_check("pipeline.read", auth, db, context)

    pipeline_def = get_pipeline_def(db, pipeline_def_id)
    if pipeline_def is None:
        raise ApiError(404, "bad_request", f"Pipeline definition {pipeline_def_id} not found")

    return envelope(
        jsonable_encoder(pipeline_def),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.patch("/defs/{pipeline_def_id}", response_model=ResponseEnvelope[PipelineDefRead])
def update_pipeline_def_route(
    pipeline_def_id: UUID,
    payload: PipelineDefUpdateRequest = Body(...),
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Update mutable fields of a pipeline definition.

    Only non-None fields are applied.
    """
    _require_idempotency(context)
    _policy_check("pipeline.update", auth, db, context)
    context = _wire_actor(context, auth)

    existing = get_pipeline_def(db, pipeline_def_id)
    if existing is None:
        raise ApiError(404, "bad_request", f"Pipeline definition {pipeline_def_id} not found")

    # Extract status as string value for DB
    status_val = payload.status.value if payload.status else None
    config_json_val = json.dumps(payload.config_json) if payload.config_json is not None else None

    row = db.execute(
        _UPDATE_PIPELINE_DEF,
        {
            "pipeline_def_id": pipeline_def_id,
            "status": status_val,
            "name": payload.name,
            "description": payload.description,
            "config_json": config_json_val,
        },
    ).first()

    if row is None:
        raise ApiError(500, "internal_error", "Pipeline definition update returned no row")

    # Re-fetch via domain layer for proper deserialization and audit
    updated = get_pipeline_def(db, pipeline_def_id)
    if updated is None:
        raise ApiError(500, "internal_error", "Pipeline definition disappeared after update")

    db.commit()

    return envelope(
        jsonable_encoder(updated),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════
# Routes — Pipeline Runs
# ═══════════════════════════════════════════════════════════════════


@router.post("/runs", response_model=ResponseEnvelope[PipelineRunRead])
def create_pipeline_run_route(
    payload: PipelineRunCreateRequest,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Manually trigger a pipeline run.

    Requires ``Idempotency-Key`` header.  The same key will not create
    duplicate runs.
    """
    if not context.idempotency_key:
        raise ApiError(
            400, "bad_request", "Idempotency-Key header is required for writes"
        )

    _policy_check("pipeline.execute", auth, db, context)
    context = _wire_actor(context, auth)

    trigger_type_str = (
        payload.trigger_type.value
        if hasattr(payload.trigger_type, "value")
        else payload.trigger_type
    )
    target_type_str = (
        payload.target_type.value
        if payload.target_type and hasattr(payload.target_type, "value")
        else payload.target_type
    )

    try:
        pipeline_run = create_pipeline_run(
            db,
            context,
            pipeline_def_id=payload.pipeline_def_id,
            trigger_type=trigger_type_str,
            target_type=target_type_str,
            target_id=payload.target_id,
            target_version=payload.target_version,
            input_json=payload.input_json,
            project_id=payload.project_id,
            trigger_event_id=payload.trigger_event_id,
        )
    except ValueError as exc:
        raise ApiError(400, "bad_request", str(exc)) from exc

    return envelope(
        jsonable_encoder(pipeline_run),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.get("/runs", response_model=ResponseEnvelope[PaginatedData[PipelineRunRead]])
def list_pipeline_runs_route(
    status: str | None = Query(None, description="Filter by run status"),
    trigger_type: str | None = Query(None, description="Filter by trigger type"),
    pipeline_def_id: UUID | None = Query(None, description="Filter by pipeline definition"),
    project_id: UUID | None = Query(None, description="Filter by project"),
    target_type: str | None = Query(None, description="Filter by target type"),
    target_id: UUID | None = Query(None, description="Filter by target ID"),
    page: int = Query(1, ge=1, description="Page number (1-based)"),
    page_size: int = Query(50, ge=1, le=200, description="Items per page"),
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """List pipeline runs with optional filters and pagination."""
    _policy_check("pipeline.read", auth, db, context)

    items, total = list_pipeline_runs(
        db,
        status=status,
        trigger_type=trigger_type,
        pipeline_def_id=pipeline_def_id,
        project_id=project_id,
        target_type=target_type,
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


@router.get("/runs/{run_id}", response_model=ResponseEnvelope[PipelineRunDetail])
def get_pipeline_run_route(
    run_id: UUID,
    include_job: bool = Query(True, description="Include root job details"),
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Get pipeline run detail with associated root job and pipeline definition."""
    _policy_check("pipeline.read", auth, db, context)

    run = get_pipeline_run(db, run_id)
    if run is None:
        raise ApiError(404, "not_found", f"Pipeline run {run_id} not found")

    # Build detail with root_job and pipeline_def
    root_job = None
    if include_job and run.root_job_id:
        root_job = get_job_by_id(run.root_job_id)

    pipeline_def = None
    if run.pipeline_def_id:
        pipeline_def = get_pipeline_def(db, run.pipeline_def_id)

    detail = PipelineRunDetail(
        pipeline_run_id=run.pipeline_run_id,
        pipeline_def_id=run.pipeline_def_id,
        project_id=run.project_id,
        root_job_id=run.root_job_id,
        trigger_type=run.trigger_type,
        trigger_event_id=run.trigger_event_id,
        target_type=run.target_type,
        target_id=run.target_id,
        target_version=run.target_version,
        status=run.status,
        started_at=run.started_at,
        finished_at=run.finished_at,
        input_json=run.input_json,
        output_json=run.output_json,
        error_json=run.error_json,
        idempotency_key=run.idempotency_key,
        created_at=run.created_at,
        updated_at=run.updated_at,
        root_job=jsonable_encoder(root_job) if root_job else None,
        pipeline_def=jsonable_encoder(pipeline_def) if pipeline_def else None,
    )

    return envelope(
        jsonable_encoder(detail),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.patch("/runs/{run_id}/status", response_model=ResponseEnvelope[PipelineRunRead])
def advance_run_status_route(
    run_id: UUID,
    new_status: str = Query(..., description="Target status: running | succeeded | failed | cancelled | superseded"),
    expected_status: str = Query(..., description="Expected current status for optimistic concurrency"),
    output_json: str | None = Query(None, description="JSON result summary (set on 'succeeded')"),
    error_json: str | None = Query(None, description="JSON error details (set on 'failed')"),
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Advance a pipeline run's status with state-machine validation.

    Valid transitions::

        pending   → running | cancelled
        running   → succeeded | failed | cancelled
        failed    → pending          (manual retry)
        succeeded → (terminal)
        cancelled → (terminal)
        superseded → (terminal)

    Uses ``expected_status`` for optimistic concurrency control.  When the
    run's current status does not match *expected_status*, the request is
    rejected with 409.
    """
    _require_idempotency(context)
    _policy_check("pipeline.execute", auth, db, context)
    context = _wire_actor(context, auth)

    # Parse optional JSON payloads
    output_dict = None
    error_dict = None
    if output_json is not None:
        try:
            output_dict = json.loads(output_json)
        except json.JSONDecodeError as exc:
            raise ApiError(400, "bad_request", f"Invalid output_json: {exc}") from exc
    if error_json is not None:
        try:
            error_dict = json.loads(error_json)
        except json.JSONDecodeError as exc:
            raise ApiError(400, "bad_request", f"Invalid error_json: {exc}") from exc

    try:
        updated_run = advance_run_status(
            db,
            context,
            pipeline_run_id=run_id,
            new_status=new_status,
            expected_status=expected_status,
            output_json=output_dict,
            error_json=error_dict,
        )
    except ValueError as exc:
        raise ApiError(400, "bad_request", str(exc)) from exc

    return envelope(
        jsonable_encoder(updated_run),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.post("/runs/{run_id}/cancel", response_model=ResponseEnvelope[PipelineRunRead])
def cancel_pipeline_run_route(
    run_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Cancel a pipeline run.

    Convenience endpoint that advances the run to ``cancelled`` status.
    The run must be in ``pending`` or ``running`` state.  Already-terminal
    runs (succeeded, failed, cancelled, superseded) are rejected.
    """
    _require_idempotency(context)
    _policy_check("pipeline.execute", auth, db, context)
    context = _wire_actor(context, auth)

    run = get_pipeline_run(db, run_id)
    if run is None:
        raise ApiError(404, "bad_request", f"Pipeline run {run_id} not found")

    current_status = (
        run.status.value if hasattr(run.status, "value") else run.status
    )

    if current_status not in ("pending", "running"):
        raise ApiError(
            400, "bad_request",
            f"Cannot cancel run in '{current_status}' status. "
            f"Only 'pending' or 'running' runs can be cancelled.",
        )

    try:
        updated_run = advance_run_status(
            db,
            context,
            pipeline_run_id=run_id,
            new_status="cancelled",
            expected_status=current_status,
        )
    except ValueError as exc:
        raise ApiError(400, "bad_request", str(exc)) from exc

    return envelope(
        jsonable_encoder(updated_run),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )
