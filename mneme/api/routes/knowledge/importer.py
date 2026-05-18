"""Importer API routes — P3-09.

Endpoints
---------
* ``POST   /api/v4/importer/dry-run``  — Validate import payload (zero side effects)
* ``POST   /api/v4/importer/preview``  — Show field mapping preview
* ``POST   /api/v4/importer/import``   — Execute formal import
* ``GET    /api/v4/importer/runs``     — List import runs
* ``GET    /api/v4/importer/runs/{id}`` — Get import run detail + report
"""

from __future__ import annotations

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
from mneme.db.importer import get_import_run, list_import_runs
from mneme.importer.engine import ImportEngine
from mneme.schemas import ResponseEnvelope
from mneme.schemas.importer import (
    ImportPayload,
    ImportReport,
    ImportRunListResponse,
    ImportRunRead,
    ImportStatus,
    PreviewResult,
    ValidationResult,
)
from mneme.security import (
    Action,
    Object,
    PolicyContext,
    actor_from_user_session,
    can,
)
from mneme.security.audit import audit_event_for_policy_denied

router = APIRouter(prefix="/importer", tags=["importer"])


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _policy_check(
    action_name: str,
    auth: AuthenticatedSession,
    db: Session,
    context: RequestContext,
) -> None:
    """Enforce policy — operator/owner can execute importer."""
    from mneme.db.audit import add_audit_event as db_add_audit_event

    policy_actor = actor_from_user_session(
        user_id=auth.user.user_id,
        role=auth.user.role_code.value,
        status=auth.user.status.value,
    )
    policy_action = Action(name=action_name)
    policy_object = Object(object_type="asset")  # importer creates assets
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
    if not context.idempotency_key:
        raise ApiError(
            400, "bad_request", "Idempotency-Key header is required for writes"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Dry Run
# ═══════════════════════════════════════════════════════════════════════════════


@router.post("/dry-run", response_model=ResponseEnvelope[ValidationResult])
def importer_dry_run(
    payload: ImportPayload,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Validate an import payload with zero side effects.

    No database writes occur.  Returns a structured validation report
    with per-item issues (errors, warnings, info).
    """
    _policy_check("asset.create", auth, db, context)

    engine = ImportEngine(db, context)
    result = engine.dry_run(payload)

    return envelope(
        jsonable_encoder(result),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Preview
# ═══════════════════════════════════════════════════════════════════════════════


@router.post("/preview", response_model=ResponseEnvelope[PreviewResult])
def importer_preview(
    payload: ImportPayload,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Preview field mapping without creating assets.

    Shows what each source item would look like as a v4.1 asset
    after mapping.  No database writes occur.
    """
    _policy_check("asset.create", auth, db, context)

    engine = ImportEngine(db, context)
    try:
        result = engine.preview(payload)
    except ValueError as exc:
        raise ApiError(400, "bad_request", str(exc)) from exc

    return envelope(
        jsonable_encoder(result),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Import (formal)
# ═══════════════════════════════════════════════════════════════════════════════


@router.post("/import", response_model=ResponseEnvelope[ImportReport])
def importer_import(
    payload: ImportPayload,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Execute a formal import.

    Creates inbox items for each source item and records a pipeline
    run to track the import.  Full asset creation is reserved for the
    Phase 5 pipeline consumer.

    Requires ``Idempotency-Key`` header.
    """
    _require_idempotency(context)
    _policy_check("asset.create", auth, db, context)
    context = _wire_actor(context, auth)

    engine = ImportEngine(db, context)
    try:
        report = engine.import_(payload)
    except ValueError as exc:
        raise ApiError(400, "bad_request", str(exc)) from exc

    # Commit the transaction
    db.commit()

    return envelope(
        jsonable_encoder(report),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# List import runs
# ═══════════════════════════════════════════════════════════════════════════════


@router.get("/runs", response_model=ResponseEnvelope[ImportRunListResponse])
def list_import_runs_endpoint(
    status: ImportStatus | None = Query(None, description="Filter by run status."),
    project_id: UUID | None = Query(None, description="Filter by project."),
    page: int = Query(1, ge=1, description="Page number (1-based)."),
    page_size: int = Query(50, ge=1, le=200, description="Items per page."),
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """List import runs (backed by pipeline_runs with trigger_type='importer')."""
    _policy_check("asset.read", auth, db, context)

    status_str = status.value if status and hasattr(status, "value") else status

    runs, total = list_import_runs(
        db,
        status=status_str,
        project_id=project_id,
        page=page,
        page_size=page_size,
    )

    from mneme.schemas.common import PageInfo
    total_pages = (total + page_size - 1) // page_size if total > 0 else 0
    result = ImportRunListResponse(
        items=list(runs),
        page_info=PageInfo(
            page=page,
            page_size=page_size,
            total_items=total,
            total_pages=total_pages,
            has_next=page < total_pages,
            has_previous=page > 1,
        ),
    )

    return envelope(
        jsonable_encoder(result),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Get import run detail
# ═══════════════════════════════════════════════════════════════════════════════


@router.get("/runs/{run_id}", response_model=ResponseEnvelope[ImportReport])
def get_import_run_endpoint(
    run_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Get import run details with full report.

    Returns the structured import report from ``pipeline_runs.output_json``
    along with the current run status.
    """
    _policy_check("asset.read", auth, db, context)

    run = get_import_run(db, run_id)
    if run is None:
        raise ApiError(404, "bad_request", f"Import run {run_id} not found")

    # Reconstruct report from pipeline_run data
    output = run.output_json or {}
    input_data = run.input_json or {}

    from mneme.schemas.importer import ImportItemResult, ImportSourceType

    # Parse items from output
    item_dicts = output.get("items", [])
    item_results = [
        ImportItemResult(
            index=it.get("index", i),
            legacy_id=it.get("legacy_id", ""),
            status=it.get("status", "skipped"),
            asset_id=UUID(it["asset_id"]) if it.get("asset_id") else None,
            asset_uid=it.get("asset_uid"),
            error=it.get("error"),
        )
        for i, it in enumerate(item_dicts)
    ]

    source_type_str = input_data.get("source_type", "mneme2_item")
    source_type = ImportSourceType(source_type_str) if source_type_str in ImportSourceType.__members__ else ImportSourceType.mneme2_item

    report = ImportReport(
        run_id=run_id,
        project_id=run.project_id,
        source_type=source_type,
        status=run.status,
        started_at=run.started_at,
        finished_at=run.finished_at,
        total_items=output.get("total_items", len(item_results)),
        succeeded=output.get("succeeded", 0),
        failed=output.get("failed", 0),
        skipped=output.get("skipped", 0),
        items=item_results,
        summary=output.get("summary", ""),
    )

    return envelope(
        jsonable_encoder(report),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )
