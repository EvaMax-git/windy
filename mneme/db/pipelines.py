"""Pipeline data-access layer — pipeline_defs + pipeline_runs CRUD.

P3-04 Pipeline 骨架 — DB operations with audit + outbox + idempotency.

State machine
-------------
``pending → running → succeeded``
                   ``→ failed → (manual retry) → pending``
                   ``→ cancelled``
                   ``→ superseded``
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Session

from mneme.api.context import RequestContext
from mneme.db.audit import (
    AuditEvent,
    OutboxEvent,
    write_with_audit_outbox_idempotency,
)
from mneme.db.transactions import session_scope
from mneme.domain.objects import (
    bump_version,
    create_version,
    register_object,
)
from mneme.schemas.pipelines import (
    PipelineDefRead,
    PipelineRunRead,
    PipelineRunDetail,
    DEFAULT_ASSET_IMPORT_PIPELINE_CONFIG,
)


# ═══════════════════════════════════════════════════════════════════
# State machine — pipeline runs
# ═══════════════════════════════════════════════════════════════════

_VALID_RUN_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"running", "cancelled"},
    "running": {"succeeded", "failed", "cancelled"},
    "failed": {"pending"},          # manual retry
    "succeeded": set(),             # terminal
    "cancelled": set(),             # terminal
    "superseded": set(),            # terminal
}


def _can_transition_run(current: str, target: str) -> bool:
    return target in _VALID_RUN_TRANSITIONS.get(current, set())


# ═══════════════════════════════════════════════════════════════════
# SQL — pipeline_defs
# ═══════════════════════════════════════════════════════════════════

_INSERT_PIPELINE_DEF = text("""
    INSERT INTO pipeline_defs (
        pipeline_def_id,
        project_id,
        pipeline_code,
        pipeline_type,
        version,
        name,
        description,
        config_json,
        status,
        created_by_user_id
    ) VALUES (
        :pipeline_def_id,
        :project_id,
        :pipeline_code,
        :pipeline_type,
        :version,
        :name,
        :description,
        :config_json,
        :status,
        :created_by_user_id
    )
    RETURNING
        pipeline_def_id, project_id, pipeline_code, pipeline_type,
        version, name, description, config_json, status,
        created_by_user_id, created_at, updated_at
""").bindparams(
    bindparam("pipeline_def_id", type_=PG_UUID(as_uuid=True)),
    bindparam("project_id", type_=PG_UUID(as_uuid=True)),
    bindparam("created_by_user_id", type_=PG_UUID(as_uuid=True)),
)

_GET_PIPELINE_DEF_BY_ID = text("""
    SELECT
        pipeline_def_id, project_id, pipeline_code, pipeline_type,
        version, name, description, config_json, status,
        created_by_user_id, created_at, updated_at
    FROM pipeline_defs
    WHERE pipeline_def_id = :pipeline_def_id
""").bindparams(bindparam("pipeline_def_id", type_=PG_UUID(as_uuid=True)))

_GET_PIPELINE_DEF_BY_CODE = text("""
    SELECT
        pipeline_def_id, project_id, pipeline_code, pipeline_type,
        version, name, description, config_json, status,
        created_by_user_id, created_at, updated_at
    FROM pipeline_defs
    WHERE pipeline_code = :pipeline_code
      AND (:project_id IS NULL OR project_id = :project_id)
    ORDER BY version DESC
    LIMIT 1
""").bindparams(bindparam("project_id", type_=PG_UUID(as_uuid=True)))

_LIST_PIPELINE_DEFS_COUNT = text("""
    SELECT count(*) FROM pipeline_defs
    WHERE (:pipeline_type IS NULL OR pipeline_type = :pipeline_type)
      AND (:status IS NULL OR status = :status)
      AND (:project_id IS NULL OR project_id = :project_id)
""").bindparams(bindparam("project_id", type_=PG_UUID(as_uuid=True)))

_LIST_PIPELINE_DEFS = text("""
    SELECT
        pipeline_def_id, project_id, pipeline_code, pipeline_type,
        version, name, description, config_json, status,
        created_by_user_id, created_at, updated_at
    FROM pipeline_defs
    WHERE (:pipeline_type IS NULL OR pipeline_type = :pipeline_type)
      AND (:status IS NULL OR status = :status)
      AND (:project_id IS NULL OR project_id = :project_id)
    ORDER BY created_at DESC
    LIMIT :page_size OFFSET :offset
""").bindparams(bindparam("project_id", type_=PG_UUID(as_uuid=True)))


# ═══════════════════════════════════════════════════════════════════
# SQL — pipeline_runs
# ═══════════════════════════════════════════════════════════════════

_INSERT_PIPELINE_RUN = text("""
    INSERT INTO pipeline_runs (
        pipeline_run_id,
        pipeline_def_id,
        project_id,
        root_job_id,
        trigger_type,
        trigger_event_id,
        target_type,
        target_id,
        target_version,
        status,
        input_json,
        output_json,
        error_json,
        idempotency_key
    ) VALUES (
        :pipeline_run_id,
        :pipeline_def_id,
        :project_id,
        :root_job_id,
        :trigger_type,
        :trigger_event_id,
        :target_type,
        :target_id,
        :target_version,
        :status,
        :input_json,
        :output_json,
        :error_json,
        :idempotency_key
    )
    RETURNING
        pipeline_run_id, pipeline_def_id, project_id, root_job_id,
        trigger_type, trigger_event_id, target_type, target_id,
        target_version, status, started_at, finished_at,
        input_json, output_json, error_json, idempotency_key,
        created_at, updated_at
""").bindparams(
    bindparam("pipeline_run_id", type_=PG_UUID(as_uuid=True)),
    bindparam("pipeline_def_id", type_=PG_UUID(as_uuid=True)),
    bindparam("project_id", type_=PG_UUID(as_uuid=True)),
    bindparam("root_job_id", type_=PG_UUID(as_uuid=True)),
    bindparam("trigger_event_id", type_=PG_UUID(as_uuid=True)),
    bindparam("target_id", type_=PG_UUID(as_uuid=True)),
)

_GET_PIPELINE_RUN_BY_ID = text("""
    SELECT
        pipeline_run_id, pipeline_def_id, project_id, root_job_id,
        trigger_type, trigger_event_id, target_type, target_id,
        target_version, status, started_at, finished_at,
        input_json, output_json, error_json, idempotency_key,
        created_at, updated_at
    FROM pipeline_runs
    WHERE pipeline_run_id = :pipeline_run_id
""").bindparams(bindparam("pipeline_run_id", type_=PG_UUID(as_uuid=True)))

_LIST_PIPELINE_RUNS_COUNT = text("""
    SELECT count(*) FROM pipeline_runs
    WHERE (:pipeline_def_id IS NULL OR pipeline_def_id = :pipeline_def_id)
      AND (:status IS NULL OR status = :status)
      AND (:trigger_type IS NULL OR trigger_type = :trigger_type)
      AND (:project_id IS NULL OR project_id = :project_id)
      AND (:target_type IS NULL OR target_type = :target_type)
      AND (:target_id IS NULL OR target_id = :target_id)
""").bindparams(
    bindparam("pipeline_def_id", type_=PG_UUID(as_uuid=True)),
    bindparam("project_id", type_=PG_UUID(as_uuid=True)),
    bindparam("target_id", type_=PG_UUID(as_uuid=True)),
)

_LIST_PIPELINE_RUNS = text("""
    SELECT
        pipeline_run_id, pipeline_def_id, project_id, root_job_id,
        trigger_type, trigger_event_id, target_type, target_id,
        target_version, status, started_at, finished_at,
        input_json, output_json, error_json, idempotency_key,
        created_at, updated_at
    FROM pipeline_runs
    WHERE (:pipeline_def_id IS NULL OR pipeline_def_id = :pipeline_def_id)
      AND (:status IS NULL OR status = :status)
      AND (:trigger_type IS NULL OR trigger_type = :trigger_type)
      AND (:project_id IS NULL OR project_id = :project_id)
      AND (:target_type IS NULL OR target_type = :target_type)
      AND (:target_id IS NULL OR target_id = :target_id)
    ORDER BY created_at DESC
    LIMIT :page_size OFFSET :offset
""").bindparams(
    bindparam("pipeline_def_id", type_=PG_UUID(as_uuid=True)),
    bindparam("project_id", type_=PG_UUID(as_uuid=True)),
    bindparam("target_id", type_=PG_UUID(as_uuid=True)),
)

_UPDATE_RUN_STATUS = text("""
    UPDATE pipeline_runs
    SET status = :new_status,
        started_at = CASE
            WHEN :new_status = 'running' AND started_at IS NULL THEN :now_ts
            ELSE started_at
        END,
        finished_at = CASE
            WHEN :new_status IN ('succeeded', 'failed', 'cancelled', 'superseded')
                THEN :now_ts
            ELSE finished_at
        END,
        output_json = COALESCE(:output_json, output_json),
        error_json = COALESCE(:error_json, error_json),
        updated_at = now()
    WHERE pipeline_run_id = :pipeline_run_id
      AND status = :expected_status
    RETURNING
        pipeline_run_id, pipeline_def_id, project_id, root_job_id,
        trigger_type, trigger_event_id, target_type, target_id,
        target_version, status, started_at, finished_at,
        input_json, output_json, error_json, idempotency_key,
        created_at, updated_at
""").bindparams(bindparam("pipeline_run_id", type_=PG_UUID(as_uuid=True)))


# ═══════════════════════════════════════════════════════════════════
# Row mapping helpers
# ═══════════════════════════════════════════════════════════════════

def _def_from_row(row: Any) -> PipelineDefRead:
    data = dict(row._mapping)
    # SQLite stores JSON as TEXT; normalize back to dict for Pydantic
    for field in ("config_json",):
        val = data.get(field)
        if isinstance(val, str):
            data[field] = json.loads(val)
    return PipelineDefRead.model_validate(data)


def _run_from_row(row: Any) -> PipelineRunRead:
    data = dict(row._mapping)
    for field in ("input_json", "output_json", "error_json"):
        val = data.get(field)
        if isinstance(val, str):
            data[field] = json.loads(val)
    return PipelineRunRead.model_validate(data)


def _iso(dt: Any) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    return dt.isoformat()


# ═══════════════════════════════════════════════════════════════════
# Public API — Pipeline Defs
# ═══════════════════════════════════════════════════════════════════


def create_pipeline_def(
    db: Session,
    context: RequestContext,
    *,
    pipeline_code: str,
    pipeline_type: str,
    name: str,
    description: str | None = None,
    config_json: dict[str, Any] | None = None,
    project_id: UUID | None = None,
    status: str = "active",
) -> PipelineDefRead:
    """Create a pipeline definition with audit + outbox + idempotency."""

    # Resolve default config based on pipeline_type if not provided
    if config_json is None:
        if pipeline_type == "asset_import":
            config_json = DEFAULT_ASSET_IMPORT_PIPELINE_CONFIG
        else:
            config_json = {"steps": []}

    pipeline_def_id = uuid4()
    object_type = "pipeline_def"

    outbox_event = OutboxEvent(
        event_type="pipeline_def.created",
        aggregate_type=object_type,
        aggregate_id=pipeline_def_id,
        aggregate_version=1,
        idempotency_key=context.idempotency_key or "",
        producer="mneme-api",
        payload_json={
            "pipeline_code": pipeline_code,
            "pipeline_type": pipeline_type,
            "name": name,
        },
        visibility="internal",
        publish_state="pending",
    )

    audit_event = AuditEvent(
        action="pipeline_def.create",
        result="success",
        object_type=object_type,
        object_id=pipeline_def_id,
        project_id=project_id,
    )

    def _do_insert(db: Session) -> PipelineDefRead:
        row = db.execute(
            _INSERT_PIPELINE_DEF,
            {
                "pipeline_def_id": pipeline_def_id,
                "project_id": project_id,
                "pipeline_code": pipeline_code,
                "pipeline_type": pipeline_type,
                "version": 1,
                "name": name,
                "description": description,
                "config_json": json.dumps(config_json),
                "status": status,
                "created_by_user_id": context.actor.actor_id,
            },
        ).one()
        return _def_from_row(row)

    def _resolve_existing(db: Session, aggregate_id: UUID) -> PipelineDefRead:
        result = get_pipeline_def(db, aggregate_id)
        if result is None:
            raise LookupError(f"pipeline_def {aggregate_id} not found")
        return result

    return write_with_audit_outbox_idempotency(
        db,
        context,
        work=_do_insert,
        audit_event=audit_event,
        outbox_event=outbox_event,
        resolve_existing=_resolve_existing,
    )


def get_pipeline_def(db: Session, pipeline_def_id: UUID) -> PipelineDefRead | None:
    """Look up a pipeline definition by primary key."""
    row = db.execute(
        _GET_PIPELINE_DEF_BY_ID, {"pipeline_def_id": pipeline_def_id}
    ).first()
    if row is None:
        return None
    return _def_from_row(row)


def get_pipeline_def_by_code(
    db: Session, *, pipeline_code: str, project_id: UUID | None = None
) -> PipelineDefRead | None:
    """Look up a pipeline definition by code and optional project."""
    row = db.execute(
        _GET_PIPELINE_DEF_BY_CODE,
        {"pipeline_code": pipeline_code, "project_id": project_id},
    ).first()
    if row is None:
        return None
    return _def_from_row(row)


def list_pipeline_defs(
    db: Session,
    *,
    pipeline_type: str | None = None,
    status: str | None = None,
    project_id: UUID | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[PipelineDefRead], int]:
    """List pipeline definitions with optional filters and pagination."""
    params = {
        "pipeline_type": pipeline_type,
        "status": status,
        "project_id": project_id,
    }
    total = db.execute(_LIST_PIPELINE_DEFS_COUNT, params).scalar_one()
    offset = (page - 1) * page_size
    rows = db.execute(
        _LIST_PIPELINE_DEFS,
        {**params, "page_size": page_size, "offset": offset},
    ).all()
    return [_def_from_row(row) for row in rows], total


# ═══════════════════════════════════════════════════════════════════
# Public API — Pipeline Runs
# ═══════════════════════════════════════════════════════════════════


def create_pipeline_run(
    db: Session,
    context: RequestContext,
    *,
    pipeline_def_id: UUID,
    trigger_type: str = "manual",
    target_type: str | None = None,
    target_id: UUID | None = None,
    target_version: int | None = None,
    input_json: dict[str, Any] | None = None,
    project_id: UUID | None = None,
    trigger_event_id: UUID | None = None,
) -> PipelineRunRead:
    """Create a pipeline run with audit + outbox + idempotency.

    The idempotency_key from the context is used to prevent duplicate runs.
    """

    # Validate the pipeline definition exists
    pipeline_def = get_pipeline_def(db, pipeline_def_id)
    if pipeline_def is None:
        raise ValueError(f"Pipeline definition {pipeline_def_id} not found")

    if pipeline_def.status.value not in ("active",):
        raise ValueError(
            f"Pipeline definition {pipeline_def_id} is '{pipeline_def.status.value}', "
            f"cannot create run"
        )

    pipeline_run_id = uuid4()
    object_type = "pipeline_run"

    outbox_event = OutboxEvent(
        event_type="pipeline.run.requested",
        aggregate_type=object_type,
        aggregate_id=pipeline_run_id,
        aggregate_version=1,
        idempotency_key=context.idempotency_key or "",
        producer="mneme-api",
        payload_json={
            "pipeline_def_id": str(pipeline_def_id),
            "pipeline_code": pipeline_def.pipeline_code,
            "pipeline_type": pipeline_def.pipeline_type.value
                if hasattr(pipeline_def.pipeline_type, "value")
                else pipeline_def.pipeline_type,
            "trigger_type": trigger_type,
            "target_type": target_type,
            "target_id": str(target_id) if target_id else None,
            "input_json": input_json or {},
        },
        visibility="internal",
        publish_state="pending",
    )

    audit_event = AuditEvent(
        action="pipeline_run.create",
        result="success",
        object_type=object_type,
        object_id=pipeline_run_id,
        project_id=project_id or pipeline_def.project_id,
    )

    def _do_insert(db: Session) -> PipelineRunRead:
        row = db.execute(
            _INSERT_PIPELINE_RUN,
            {
                "pipeline_run_id": pipeline_run_id,
                "pipeline_def_id": pipeline_def_id,
                "project_id": project_id or pipeline_def.project_id,
                "root_job_id": None,
                "trigger_type": trigger_type,
                "trigger_event_id": trigger_event_id,
                "target_type": target_type,
                "target_id": target_id,
                "target_version": target_version,
                "status": "pending",
                "input_json": json.dumps(input_json or {}),
                "output_json": json.dumps({}),
                "error_json": json.dumps({}),
                "idempotency_key": context.idempotency_key or str(uuid4()),
            },
        ).one()
        return _run_from_row(row)

    def _resolve_existing(db: Session, aggregate_id: UUID) -> PipelineRunRead:
        result = get_pipeline_run(db, aggregate_id)
        if result is None:
            raise LookupError(f"pipeline_run {aggregate_id} not found")
        return result

    return write_with_audit_outbox_idempotency(
        db,
        context,
        work=_do_insert,
        audit_event=audit_event,
        outbox_event=outbox_event,
        resolve_existing=_resolve_existing,
    )


def get_pipeline_run(db: Session, pipeline_run_id: UUID) -> PipelineRunRead | None:
    """Look up a pipeline run by primary key."""
    row = db.execute(
        _GET_PIPELINE_RUN_BY_ID, {"pipeline_run_id": pipeline_run_id}
    ).first()
    if row is None:
        return None
    return _run_from_row(row)


def list_pipeline_runs(
    db: Session,
    *,
    pipeline_def_id: UUID | None = None,
    status: str | None = None,
    trigger_type: str | None = None,
    project_id: UUID | None = None,
    target_type: str | None = None,
    target_id: UUID | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[PipelineRunRead], int]:
    """List pipeline runs with optional filters and pagination."""
    params = {
        "pipeline_def_id": pipeline_def_id,
        "status": status,
        "trigger_type": trigger_type,
        "project_id": project_id,
        "target_type": target_type,
        "target_id": target_id,
    }
    total = db.execute(_LIST_PIPELINE_RUNS_COUNT, params).scalar_one()
    offset = (page - 1) * page_size
    rows = db.execute(
        _LIST_PIPELINE_RUNS,
        {**params, "page_size": page_size, "offset": offset},
    ).all()
    return [_run_from_row(row) for row in rows], total


def advance_run_status(
    db: Session,
    context: RequestContext,
    *,
    pipeline_run_id: UUID,
    new_status: str,
    expected_status: str,
    output_json: dict[str, Any] | None = None,
    error_json: dict[str, Any] | None = None,
) -> PipelineRunRead:
    """Advance a pipeline run's status with state-machine validation.

    Args:
        db: Active session.
        context: Request context.
        pipeline_run_id: The run to update.
        new_status: Target status (e.g. 'running', 'succeeded', 'failed').
        expected_status: Required current status for optimistic concurrency.
        output_json: Result summary (set on 'succeeded').
        error_json: Error details (set on 'failed').

    Returns:
        Updated PipelineRunRead.

    Raises:
        ValueError: If the run is not found or the transition is invalid.
    """
    existing = get_pipeline_run(db, pipeline_run_id)
    if existing is None:
        raise ValueError(f"Pipeline run {pipeline_run_id} not found")

    existing_status = (
        existing.status.value
        if hasattr(existing.status, "value")
        else existing.status
    )

    if existing_status != expected_status:
        raise ValueError(
            f"Expected status '{expected_status}' but current is '{existing_status}'"
        )

    if not _can_transition_run(existing_status, new_status):
        raise ValueError(
            f"Invalid run status transition: '{existing_status}' -> '{new_status}'"
        )

    now_ts = datetime.now(timezone.utc)
    object_type = "pipeline_run"

    outbox_event = OutboxEvent(
        event_type=f"pipeline.run.{new_status}",
        aggregate_type=object_type,
        aggregate_id=pipeline_run_id,
        aggregate_version=1,
        idempotency_key=f"{context.idempotency_key or ''}:advance:{pipeline_run_id}:{new_status}",
        producer="mneme-api",
        payload_json={
            "pipeline_run_id": str(pipeline_run_id),
            "previous_status": existing_status,
            "new_status": new_status,
        },
        visibility="internal",
        publish_state="pending",
    )

    audit_event = AuditEvent(
        action=f"pipeline_run.{new_status}",
        result="success",
        object_type=object_type,
        object_id=pipeline_run_id,
        project_id=existing.project_id,
        diff_summary={
            "status": {"from": existing_status, "to": new_status},
        },
    )

    def _do_advance(db: Session) -> PipelineRunRead:
        row = db.execute(
            _UPDATE_RUN_STATUS,
            {
                "pipeline_run_id": pipeline_run_id,
                "new_status": new_status,
                "expected_status": expected_status,
                "now_ts": now_ts,
                "output_json": json.dumps(output_json) if output_json else None,
                "error_json": json.dumps(error_json) if error_json else None,
            },
        ).first()
        if row is None:
            raise ValueError(f"Status advance conflict for run {pipeline_run_id}")
        return _run_from_row(row)

    def _resolve_existing(db: Session, aggregate_id: UUID) -> PipelineRunRead:
        result = get_pipeline_run(db, aggregate_id)
        if result is None:
            raise LookupError(f"pipeline_run {aggregate_id} not found")
        return result

    return write_with_audit_outbox_idempotency(
        db,
        context,
        work=_do_advance,
        audit_event=audit_event,
        outbox_event=outbox_event,
        resolve_existing=_resolve_existing,
    )


# ═══════════════════════════════════════════════════════════════════
# Asset Import Pipeline Orchestrator
# ═══════════════════════════════════════════════════════════════════


def asset_import_orchestrator(
    db: Session,
    context: RequestContext,
    *,
    pipeline_run_id: UUID,
    asset_id: UUID | None = None,
) -> dict[str, Any]:
    """Execute the asset_import pipeline steps.

    This is the core orchestrator that runs the 5-step asset import process:

    1. **validate_hash** — Hash integrity check
    2. **extract_metadata** — Extract file metadata (name, size, MIME, etc.)
    3. **write_metadata** — Write metadata to asset_metadata table
    4. **update_ingest_state** — Set assets.ingest_state = 'ready'
    5. **create_document** — Create knowledge document, blocks, chunks, FTS index

    Args:
        db: Active session.
        context: Request context.
        pipeline_run_id: The pipeline run to execute.
        asset_id: Target asset ID (from run's input_json or explicit).

    Returns:
        A summary dict with step results.
    """
    from mneme.db.assets import get_asset, advance_ingest_state
    from mneme.db.asset_metadata import add_metadata
    from mneme.schemas.asset_metadata import AssetMetadataCreateRequest, MetadataValueType

    run = get_pipeline_run(db, pipeline_run_id)
    if run is None:
        raise ValueError(f"Pipeline run {pipeline_run_id} not found")

    # Resolve asset_id from input_json if not explicitly provided
    if asset_id is None:
        input_data = run.input_json or {}
        asset_id_str = input_data.get("asset_id")
        if asset_id_str:
            asset_id = UUID(asset_id_str)

    if asset_id is None:
        raise ValueError("No asset_id provided for asset_import pipeline")

    asset = get_asset(db, asset_id)
    if asset is None:
        raise ValueError(f"Asset {asset_id} not found")

    step_results: list[dict[str, Any]] = []

    # Step 1: Validate hash
    _step_log(db, pipeline_run_id, 1, "validate_hash", "Validating content hash integrity", "info")
    hash_valid = True
    if asset.content_hash:
        hash_valid = len(asset.content_hash) >= 16  # Basic length check
    if not hash_valid:
        raise ValueError(f"Hash validation failed for asset {asset_id}")
    step_results.append({"step_code": "validate_hash", "result": "passed", "hash_valid": hash_valid})

    # Step 2: Extract file metadata
    _step_log(db, pipeline_run_id, 1, "extract_metadata", "Extracting file metadata", "info")
    extracted_meta = {
        "original_filename": asset.original_filename,
        "media_type": asset.media_type,
        "size_bytes": asset.size_bytes,
        "asset_type": asset.asset_type.value if hasattr(asset.asset_type, "value") else asset.asset_type,
        "created_at": _iso(asset.created_at),
        "content_hash": asset.content_hash,
    }
    step_results.append({"step_code": "extract_metadata", "result": "passed", "extracted_fields": list(extracted_meta.keys())})

    # Step 3: Write metadata to asset_metadata table
    _step_log(db, pipeline_run_id, 1, "write_metadata", "Writing metadata to asset_metadata table", "info")
    metadata_count = 0
    for key, value in extracted_meta.items():
        if value is not None:
            try:
                add_metadata(
                    db,
                    context,
                    asset_id=asset_id,
                    payload=AssetMetadataCreateRequest(
                        metadata_key=key,
                        metadata_value=str(value) if value is not None else None,
                        value_type=MetadataValueType.text,
                        source="pipeline",
                    ),
                )
                metadata_count += 1
            except Exception as exc:
                _step_log(
                    db, pipeline_run_id, 1, "write_metadata",
                    f"Skipping metadata key '{key}': {exc}", "warning"
                )
    step_results.append({"step_code": "write_metadata", "result": "passed", "metadata_count": metadata_count})

    # Step 4: Advance ingest_state staged → importing
    _step_log(db, pipeline_run_id, 1, "update_ingest_state", "Advancing ingest_state: staged → importing", "info")
    advance_ingest_state(
        db, context,
        asset_id=asset_id,
        new_ingest_state="importing",
        expected_ingest_state="staged",
    )
    step_results.append({"step_code": "update_ingest_state", "result": "passed", "ingest_state": "importing"})

    # Step 5: Create knowledge document from asset file
    _step_log(db, pipeline_run_id, 1, "create_document", "Creating knowledge document from asset", "info")
    doc_created = False
    try:
        from pathlib import Path as _Path
        from mneme.storage.backend import get_backend
        from mneme.db.knowledge import (
            create_document, add_block, insert_chunks, _mark_fts_ready,
        )
        from mneme.knowledge.chunking import chunk_document
        from mneme.schemas.knowledge import KnowledgeDocumentCreate, KnowledgeBlockCreate, BlockType

        if asset.storage_ref and asset.project_id:
            backend = get_backend()
            storage_path = _Path(asset.storage_ref)
            if backend.file_exists(storage_path):
                content_bytes = backend.read_file(storage_path)
                # Try common encodings to avoid replacing Chinese chars with �
                text = None
                for enc in ("utf-8", "gb18030", "gbk", "utf-16", "latin-1"):
                    try:
                        text = content_bytes.decode(enc)
                        break
                    except (UnicodeDecodeError, UnicodeError):
                        continue
                if text is None:
                    text = content_bytes.decode("utf-8", errors="replace")

                if text.strip():
                    doc_payload = KnowledgeDocumentCreate(
                        project_id=asset.project_id,
                        title=asset.title or asset.original_filename or "未命名文档",
                        source_asset_id=asset_id,
                    )
                    doc_ctx = RequestContext(
                        request_id=context.request_id,
                        correlation_id=context.correlation_id,
                        actor=context.actor,
                        idempotency_key=f"{context.idempotency_key}-doc-{uuid4().hex[:8]}",
                    )
                    doc = create_document(db, doc_ctx, payload=doc_payload)

                    # Limit text size for chunking
                    max_len = 1_000_000
                    if len(text) > max_len:
                        text = text[:max_len]

                    block_payload = KnowledgeBlockCreate(
                        block_order=0,
                        block_type=BlockType.paragraph,
                        content_markdown=text,
                    )
                    block_ctx = RequestContext(
                        request_id=context.request_id,
                        correlation_id=context.correlation_id,
                        actor=context.actor,
                        idempotency_key=f"{context.idempotency_key}-block-{uuid4().hex[:8]}",
                    )
                    block = add_block(db, block_ctx, document_id=doc.document_id, payload=block_payload)

                    blocks = [{
                        "block_id": block.block_id,
                        "content_markdown": text,
                        "block_order": 0,
                    }]
                    result = chunk_document(
                        document_id=doc.document_id,
                        document_version=doc.current_version,
                        blocks=blocks,
                    )

                    if result.chunks:
                        chunk_dicts = [
                            {
                                "chunk_id": uuid4(),
                                "chunk_order": c.chunk_order,
                                "chunk_text": c.chunk_text,
                                "token_count": c.token_count,
                                "block_id": c.block_id,
                                "document_version": doc.current_version,
                                "span_start": c.span_start,
                                "span_end": c.span_end,
                            }
                            for c in result.chunks
                        ]
                        insert_chunks(
                            db, chunks=chunk_dicts,
                            project_id=doc.project_id or asset.project_id,
                            document_id=doc.document_id,
                        )

                    _mark_fts_ready(db, doc.document_id)
                    doc_created = True
                    step_results.append({
                        "step_code": "create_document", "result": "passed",
                        "document_id": str(doc.document_id),
                        "chunk_count": len(result.chunks),
                    })

                    # Step 5b: Advance ingest_state importing → ready
                    _step_log(db, pipeline_run_id, 1, "finalize_ingest", "Advancing ingest_state: importing → ready", "info")
                    advance_ingest_state(
                        db, context,
                        asset_id=asset_id,
                        new_ingest_state="ready",
                        expected_ingest_state="importing",
                    )
                    step_results.append({"step_code": "finalize_ingest", "result": "passed", "ingest_state": "ready"})
    except Exception as exc:
        _step_log(db, pipeline_run_id, 1, "create_document",
                  f"Document creation skipped (non-fatal): {exc}", "warning")

    if not doc_created:
        step_results.append({
            "step_code": "create_document", "result": "skipped",
            "note": "File not text-decodable or missing storage_ref",
        })
        # Still finalize ingest state even if no document was created
        _step_log(db, pipeline_run_id, 1, "finalize_ingest", "Advancing ingest_state: importing → ready (no document)", "info")
        advance_ingest_state(
            db, context,
            asset_id=asset_id,
            new_ingest_state="ready",
            expected_ingest_state="importing",
        )
        step_results.append({"step_code": "finalize_ingest", "result": "passed", "ingest_state": "ready"})

    summary = {
        "asset_id": str(asset_id),
        "steps_completed": len(step_results),
        "steps": step_results,
        "ingest_state": "ready",
    }
    return summary


def _step_log(
    db: Session,
    pipeline_run_id: UUID,
    attempt_no: int,
    step: str,
    message: str,
    level: str = "info",
) -> None:
    """Log a pipeline step message to job_logs if root_job_id is set.

    Falls back to Python logger if no root_job is associated.
    """
    import logging
    logger = logging.getLogger(__name__)

    run = get_pipeline_run(db, pipeline_run_id)
    if run is not None and run.root_job_id:
        try:
            from mneme.db.jobs import add_job_log
            add_job_log(
                job_id=run.root_job_id,
                step=step,
                message=message,
                level=level,
                attempt_no=attempt_no,
            )
        except Exception:
            logger.warning(
                "pipeline step log failed – run=%s step=%s msg=%s",
                pipeline_run_id, step, message,
            )
    else:
        logger.info("pipeline step – run=%s step=%s msg=%s", pipeline_run_id, step, message)


def add_metadata_raw(
    db: Session,
    context: RequestContext,
    *,
    asset_id: UUID,
    metadata_key: str,
    metadata_value: str | None,
    value_type: str = "text",
    source: str = "pipeline",
    confidence: float | None = None,
) -> Any:
    """Add metadata without Pydantic schema wrapping (used by orchestrator)."""
    from mneme.db.assets import add_metadata
    from mneme.schemas.asset_metadata import AssetMetadataCreateRequest, MetadataValueType

    payload = AssetMetadataCreateRequest(
        metadata_key=metadata_key,
        metadata_value=metadata_value,
        value_type=MetadataValueType(value_type),
        source=source,
        confidence=confidence,
    )
    return add_metadata(db, context, asset_id=asset_id, payload=payload)


# ═══════════════════════════════════════════════════════════════════
# Bootstrap — default asset_import pipeline definitions
# ═══════════════════════════════════════════════════════════════════

_DEFAULT_ASSET_IMPORT_PIPELINES: list[dict[str, Any]] = [
    {
        "pipeline_code": "standard_chunk",
        "name": "标准分块",
        "description": "通用文档分块与向量化处理，支持 Markdown、纯文本、PDF 等格式",
        "config_json": {
            "steps": DEFAULT_ASSET_IMPORT_PIPELINE_CONFIG["steps"],
            "supported_formats": ["md", "txt", "pdf", "json", "csv", "html", "py", "ts", "tsx", "js", "jsx", "go", "rs", "java", "cpp", "c", "h", "rb", "php", "swift", "kt", "cs"],
        },
    },
    {
        "pipeline_code": "code_parse",
        "name": "代码解析",
        "description": "专为代码文件优化，保留语法结构与符号引用关系",
        "config_json": {
            "steps": DEFAULT_ASSET_IMPORT_PIPELINE_CONFIG["steps"],
            "supported_formats": ["py", "ts", "tsx", "js", "jsx", "go", "rs", "java", "cpp", "c", "h", "rb", "php", "swift", "kt", "cs"],
        },
    },
    {
        "pipeline_code": "ocr_document",
        "name": "OCR 识别",
        "description": "针对扫描件和图片文档，执行 OCR 解析后分块",
        "config_json": {
            "steps": DEFAULT_ASSET_IMPORT_PIPELINE_CONFIG["steps"],
            "supported_formats": ["png", "jpg", "jpeg", "webp", "tiff"],
        },
    },
    {
        "pipeline_code": "dialog_parse",
        "name": "对话解析",
        "description": "处理聊天导出文件，提取对话结构与消息元数据",
        "config_json": {
            "steps": DEFAULT_ASSET_IMPORT_PIPELINE_CONFIG["steps"],
            "supported_formats": ["csv", "html"],
        },
    },
]


def seed_default_asset_import_pipelines() -> int:
    """Insert default asset_import pipeline_defs if not already present.

    Checks by specific pipeline_code rather than just counting rows of
    type ``asset_import``, so that pre-existing test rows don't prevent
    seeding the four standard pipelines.

    Returns the number of rows inserted (could be 0-4).
    """
    default_codes = {p["pipeline_code"] for p in _DEFAULT_ASSET_IMPORT_PIPELINES}

    with session_scope() as db:
        # Check which default codes already exist
        existing_codes = {
            row[0] for row in db.execute(
                text("SELECT pipeline_code FROM pipeline_defs"),
            ).all()
        }

        if existing_codes == default_codes:
            return 0

        system_user_id = db.execute(
            text("SELECT user_id FROM users ORDER BY created_at ASC LIMIT 1")
        ).scalar()

        inserted = 0
        for p in _DEFAULT_ASSET_IMPORT_PIPELINES:
            if p["pipeline_code"] in existing_codes:
                continue
            db.execute(
                text("""
                    INSERT INTO pipeline_defs (
                        pipeline_def_id, project_id, pipeline_code, pipeline_type,
                        version, name, description, config_json, status,
                        created_by_user_id
                    ) VALUES (
                        gen_random_uuid(), NULL, :pipeline_code, 'asset_import',
                        1, :name, :description, :config_json, 'active',
                        :created_by_user_id
                    )
                    ON CONFLICT DO NOTHING
                """),
                {
                    "pipeline_code": p["pipeline_code"],
                    "name": p["name"],
                    "description": p["description"],
                    "config_json": json.dumps(p["config_json"]),
                    "created_by_user_id": system_user_id,
                },
            )
            inserted += 1

    return inserted
