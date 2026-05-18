"""Import API routes — knowledge import job management.

Provides the frontend-driven import flow:

* ``POST /api/v4/import`` — Upload file + create processing job → returns job_id
* ``POST /api/v4/import/by-asset`` — Create processing jobs for already-uploaded assets
* ``GET  /api/v4/import/{job_id}/status`` — Poll processing job status
* ``GET  /api/v4/import`` — List all processing jobs (paginated)
"""

from __future__ import annotations

from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from pydantic import BaseModel
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
from mneme.db.projects import get_project
from mneme.db.assets import get_asset, ingest_asset, DuplicateAssetError, lookup_asset_by_hash
from mneme.db.processing_jobs import (
    advance_job_status,
    create_processing_job,
    get_processing_job_status,
    list_processing_jobs,
)
from mneme.db.pipeline_registry import (
    get_pipeline_registry_by_module,
)
from mneme.db.pipelines import (
    create_pipeline_run,
    get_pipeline_def,
    get_pipeline_def_by_code,
    list_pipeline_defs,
)
from mneme.schemas import ResponseEnvelope
from mneme.schemas.processing_jobs import (
    ProcessingJobCreateRequest,
    ProcessingJobRead,
    ProcessingJobStatus,
)
from mneme.storage.upload import (
    handle_idempotent_upload_stream,
    validate_filename,
)
from mneme.storage.promote import PromoteError

router = APIRouter(prefix="/import", tags=["import"])


# ── Request models ─────────────────────────────────────────────────────────

class ImportJobsByAssetRequest(BaseModel):
    """Create processing jobs for already-uploaded assets (no file re-upload)."""
    asset_ids: list[UUID]
    pipeline_key: str
    target_stores: list[str] = []
    project_id: UUID | None = None


# ── Pipeline key → UUID resolution ───────────────────────────────────────

def _resolve_pipeline_id(db: Session, pipeline_key: str) -> UUID:
    """Resolve a pipeline_key (string) to a pipeline_def UUID.

    Looks up in pipeline_defs by UUID or by pipeline_code. Falls back to
    pipeline_registry lookup to find a matching def.
    """
    # Try direct UUID on pipeline_defs
    try:
        pid = UUID(pipeline_key)
        existing = get_pipeline_def(db, pid)
        if existing is not None:
            return pid
    except (ValueError, AttributeError):
        pass

    # Look up pipeline_def by code
    existing = get_pipeline_def_by_code(db, pipeline_code=pipeline_key)
    if existing is not None:
        return existing.pipeline_def_id

    # Fallback: try pipeline_registry, then match processor_module → def code
    existing_reg = get_pipeline_registry_by_module(db, pipeline_key)
    if existing_reg is not None:
        def_match = get_pipeline_def_by_code(db, pipeline_code=existing_reg.processor_module)
        if def_match is not None:
            return def_match.pipeline_def_id

    # Last resort: find any active asset_import pipeline def
    defs, _ = list_pipeline_defs(db, pipeline_type="asset_import", status="active", page_size=1)
    if defs:
        return defs[0].pipeline_def_id

    raise ApiError(
        400, "bad_request",
        f"无法解析管道标识: {pipeline_key}",
    )


def _wire_actor(context: RequestContext, auth: AuthenticatedSession) -> RequestContext:
    return with_actor(
        context,
        actor=ActorContext(
            actor_type="user",
            actor_id=auth.user.user_id,
            auth_context_type="user_session",
            auth_context_id=auth.session.session_id,
        ),
    )


@router.post("/by-asset", response_model=ResponseEnvelope[list[ProcessingJobRead]], status_code=201)
async def create_import_jobs_by_asset(
    payload: ImportJobsByAssetRequest,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Create processing jobs for already-uploaded assets (no file re-upload).

    Accepts a list of asset IDs that have been previously uploaded via
    ``POST /api/v4/assets/ingest``, resolves the pipeline key, and creates
    a ``processing_jobs`` record for each asset in ``queued`` status.
    """
    if not context.idempotency_key:
        raise ApiError(
            400, "bad_request", "Idempotency-Key header is required for writes"
        )

    # Validate project exists (optional)
    project = None
    if payload.project_id is not None:
        project = get_project(db, payload.project_id)
        if project is None:
            raise ApiError(404, "not_found", f"Project {payload.project_id} not found")

    # Resolve pipeline_key → UUID
    pipeline_uuid = _resolve_pipeline_id(db, payload.pipeline_key)

    context = _wire_actor(context, auth)

    # Get pipeline def for run creation
    pipeline_def = get_pipeline_def(db, pipeline_uuid)
    pipeline_type = (
        pipeline_def.pipeline_type.value
        if pipeline_def and hasattr(pipeline_def.pipeline_type, "value")
        else str(pipeline_def.pipeline_type)
    ) if pipeline_def else "asset_import"

    jobs: list[ProcessingJobRead] = []
    for asset_id in payload.asset_ids:
        # Verify asset exists (skip silently if not — caller can check result)
        asset = get_asset(db, asset_id)
        if asset is None:
            continue

        job_payload = ProcessingJobCreateRequest(
            asset_id=asset_id,
            pipeline_id=pipeline_uuid,
            target_stores=payload.target_stores,
        )
        try:
            job = create_processing_job(db, payload=job_payload)
            jobs.append(job)
        except ValueError as exc:
            raise ApiError(400, "bad_request", str(exc)) from exc

        # Create a pipeline_run to trigger async worker processing
        try:
            run_input = {
                "asset_id": str(asset_id),
                "processing_job_id": str(job.id),
                "target_stores": payload.target_stores,
                "original_filename": getattr(asset, "original_filename", None) or "",
            }
            run_context = RequestContext(
                request_id=context.request_id,
                correlation_id=context.correlation_id,
                actor=context.actor,
                idempotency_key=f"{context.idempotency_key}-run-{uuid4().hex[:8]}",
            )
            create_pipeline_run(
                db,
                run_context,
                pipeline_def_id=pipeline_uuid,
                trigger_type="api",
                target_type="asset",
                target_id=asset_id,
                target_version=1,
                input_json=run_input,
                project_id=payload.project_id,
            )
        except Exception as exc:
            import logging
            _log = logging.getLogger(__name__)
            _log.warning(
                "import/by-asset: pipeline_run creation failed for asset=%s: %s",
                asset_id, exc,
            )

    return envelope(
        jsonable_encoder(jobs),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.post("", response_model=ResponseEnvelope[ProcessingJobRead], status_code=201)
async def create_import_job(
    file: UploadFile = File(..., description="File to import"),
    pipeline_key: str = Form(..., description="Pipeline key (e.g. standard_chunk, ocr_document)"),
    target_stores: str = Form(
        "", description="Comma-separated target sub-library keys (vector, graph, fts)"
    ),
    project_id: UUID | None = Form(None, description="Target project ID (optional)"),
    title: str | None = Form(None, description="Optional asset title"),
    asset_type: str = Form("document"),
    sensitivity_level: str = Form("normal"),
    retention_policy: str = Form("default"),
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Upload a file and create a knowledge import processing job.

    This is the primary import endpoint. It performs:

    1. Validate the uploaded file.
    2. Stage the file to disk and compute its content hash.
    3. Check for duplicate assets by content hash.
    4. Create the asset record (full ingest: inbox + asset + promote).
    5. Resolve pipeline_key → pipeline_registry UUID.
    6. Create a ``processing_jobs`` record in ``queued`` status.
    7. Return the job ID for status polling.

    The actual chunking / indexing is performed asynchronously by the
    pipeline engine, which advances the job status via the internal
    ``advance_job_status`` API.
    """
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

    # Resolve project (optional)
    project = None
    project_code = None
    if project_id is not None:
        project = get_project(db, project_id)
        if project is None:
            raise ApiError(404, "not_found", f"Project {project_id} not found")
        project_code = project.project_code

    # Resolve pipeline_key → UUID
    pipeline_uuid = _resolve_pipeline_id(db, pipeline_key)

    # Parse target stores
    store_list = [s.strip() for s in target_stores.split(",") if s.strip()]

    context = _wire_actor(context, auth)

    # ── Stage the file ────────────────────────────────────────────────────

    class _DuplicateFound(Exception):
        def __init__(self, existing_asset):
            self.existing_asset = existing_asset

    def lookup_dup(content_hash: str, pid: UUID | None) -> None:
        existing = lookup_asset_by_hash(db, content_hash=content_hash, project_id=pid)
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
        # Asset already exists — create a processing job for the existing asset
        job_payload = ProcessingJobCreateRequest(
            asset_id=dup.existing_asset.asset_id,
            pipeline_id=pipeline_uuid,
            target_stores=store_list,
        )
        try:
            job = create_processing_job(db, payload=job_payload)
        except ValueError as exc:
            raise ApiError(400, "bad_request", str(exc)) from exc

        return envelope(
            jsonable_encoder(job),
            request_id=context.request_id,
            correlation_id=context.correlation_id,
            meta={"duplicate": True, "asset_uid": dup.existing_asset.asset_uid},
        )

    if upload_result.staged_info is None:
        raise ApiError(500, "internal_error", "File staging failed")

    # ── Full ingest: inbox + asset + promote + link ──────────────────────
    try:
        asset = ingest_asset(
            db,
            context,
            staged_info=upload_result.staged_info,
            project_id=project_id,
            project_code=project_code,
            title=title or file.filename,
            asset_type=asset_type,
            sensitivity_level=sensitivity_level,
            retention_policy=retention_policy,
            source="api",
            source_uri=f"file://{upload_result.staged_info.staging_path}",
        )
    except DuplicateAssetError as dup:
        job_payload = ProcessingJobCreateRequest(
            asset_id=dup.existing_asset.asset_id,
            pipeline_id=pipeline_uuid,
            target_stores=store_list,
        )
        try:
            job = create_processing_job(db, payload=job_payload)
        except ValueError as exc:
            raise ApiError(400, "bad_request", str(exc)) from exc

        return envelope(
            jsonable_encoder(job),
            request_id=context.request_id,
            correlation_id=context.correlation_id,
            meta={"duplicate": True, "asset_uid": dup.existing_asset.asset_uid},
        )
    except (ValueError, PromoteError) as exc:
        raise ApiError(400, "bad_request", str(exc)) from exc
    except Exception as exc:
        raise ApiError(500, "internal_error", f"Asset ingest failed: {exc}") from exc

    # ── Create processing job ─────────────────────────────────────────────
    job_payload = ProcessingJobCreateRequest(
        asset_id=asset.asset_id,
        pipeline_id=pipeline_uuid,
        target_stores=store_list,
    )

    try:
        job = create_processing_job(db, payload=job_payload)
    except ValueError as exc:
        raise ApiError(400, "bad_request", str(exc)) from exc

    # ── Create pipeline run to actually execute the work ──────────────────
    # The processing_job is just a tracking record; we need a pipeline_run
    # with a pipeline.run.requested outbox event to trigger the worker.
    try:
        run_input = {
            "asset_id": str(asset.asset_id),
            "processing_job_id": str(job.id),
            "target_stores": store_list,
            "original_filename": file.filename,
        }
        pipeline_def = get_pipeline_def(db, pipeline_uuid)
        pipeline_type = (
            pipeline_def.pipeline_type.value
            if hasattr(pipeline_def.pipeline_type, "value")
            else str(pipeline_def.pipeline_type)
        ) if pipeline_def else "asset_import"

        # Use a distinct idempotency key for the pipeline run
        from uuid import uuid4 as _uuid4
        run_context = RequestContext(
            request_id=context.request_id,
            correlation_id=context.correlation_id,
            actor=context.actor,
            idempotency_key=f"{context.idempotency_key}-run-{_uuid4().hex[:8]}",
        )
        create_pipeline_run(
            db,
            run_context,
            pipeline_def_id=pipeline_uuid,
            trigger_type="api",
            target_type="asset",
            target_id=asset.asset_id,
            target_version=1,
            input_json=run_input,
            project_id=project_id,
        )
    except Exception as pipeline_exc:
        # Pipeline run creation failure is non-fatal to the upload itself
        # but we log it so it can be diagnosed
        import logging
        _log = logging.getLogger(__name__)
        _log.warning(
            "import: pipeline_run creation failed for asset=%s: %s",
            asset.asset_id,
            pipeline_exc,
        )

    return envelope(
        jsonable_encoder(job),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
        meta={"asset_uid": asset.asset_uid},
    )


@router.get("/{job_id}/status", response_model=ResponseEnvelope[ProcessingJobStatus])
def get_import_job_status(
    job_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    _auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Poll the status of a knowledge import processing job.

    Returns the current status (queued/processing/done/failed), progress
    info, and any error message.
    """
    status = get_processing_job_status(db, job_id)
    if status is None:
        raise ApiError(404, "not_found", f"Import job {job_id} not found")

    return envelope(
        jsonable_encoder(status),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


@router.get("", response_model=ResponseEnvelope[list[ProcessingJobRead]])
def list_import_jobs(
    asset_id: UUID | None = Query(None, description="Filter by asset ID"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(50, ge=1, le=200, description="Items per page"),
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    _auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """List all knowledge import processing jobs."""
    items, total = list_processing_jobs(
        db, asset_id=asset_id, page=page, page_size=page_size
    )

    return envelope(
        jsonable_encoder(items),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
        meta={"total": total},
    )
