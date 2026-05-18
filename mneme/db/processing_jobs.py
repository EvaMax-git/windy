"""Processing jobs data-access layer.

Tracks the lifecycle of knowledge import jobs through the
``processing_jobs`` table: queued → processing → done/failed.

Provides CRUD + status transition helpers.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Session

from mneme.db.pg_arrays import parse_pg_array, to_pg_array
from mneme.db.transactions import transaction
from mneme.schemas.processing_jobs import (
    ProcessingJobCreateRequest,
    ProcessingJobRead,
    ProcessingJobStatus,
)


# ── SQL ────────────────────────────────────────────────────────────────────────

_INSERT_JOB = text("""
    INSERT INTO processing_jobs (
        id, asset_id, pipeline_id, target_stores, status
    ) VALUES (
        :id, :asset_id, :pipeline_id, CAST(:target_stores AS text[]), 'queued'
    )
    RETURNING id, asset_id, pipeline_id, target_stores, status,
              chunks_produced, error, started_at, completed_at, created_at
""").bindparams(
    bindparam("id", type_=PG_UUID(as_uuid=True)),
    bindparam("asset_id", type_=PG_UUID(as_uuid=True)),
    bindparam("pipeline_id", type_=PG_UUID(as_uuid=True)),
)

_SELECT_BY_ID = text("""
    SELECT id, asset_id, pipeline_id, target_stores, status,
           chunks_produced, error, started_at, completed_at, created_at
    FROM processing_jobs
    WHERE id = :id
""").bindparams(bindparam("id", type_=PG_UUID(as_uuid=True)))

_SELECT_BY_ASSET = text("""
    SELECT id, asset_id, pipeline_id, target_stores, status,
           chunks_produced, error, started_at, completed_at, created_at
    FROM processing_jobs
    WHERE asset_id = :asset_id
    ORDER BY created_at DESC
""").bindparams(bindparam("asset_id", type_=PG_UUID(as_uuid=True)))

_SELECT_ALL = text("""
    SELECT id, asset_id, pipeline_id, target_stores, status,
           chunks_produced, error, started_at, completed_at, created_at
    FROM processing_jobs
    ORDER BY created_at DESC
    LIMIT :limit OFFSET :offset
""")

_TRANSITION_STATUS = text("""
    UPDATE processing_jobs
    SET status = :new_status,
        started_at = CASE WHEN :new_status = 'processing'
                          AND started_at IS NULL THEN now() ELSE started_at END,
        completed_at = CASE WHEN :new_status IN ('done', 'failed')
                            THEN now() ELSE completed_at END,
        error = COALESCE(:error, error),
        chunks_produced = COALESCE(:chunks_produced, chunks_produced)
    WHERE id = :id
      AND status = :expected_status
    RETURNING id, asset_id, pipeline_id, target_stores, status,
              chunks_produced, error, started_at, completed_at, created_at
""").bindparams(bindparam("id", type_=PG_UUID(as_uuid=True)))


# ── Row mapping ────────────────────────────────────────────────────────────────

def _job_from_row(row) -> ProcessingJobRead:
    data = dict(row._mapping)
    data["target_stores"] = parse_pg_array(data.get("target_stores"))
    return ProcessingJobRead.model_validate(data)


def _status_from_row(row) -> ProcessingJobStatus:
    data = dict(row._mapping)
    data.pop("target_stores", None)
    # Rename id → job_id for the status model
    if "id" in data:
        data["job_id"] = data.pop("id")
    return ProcessingJobStatus.model_validate(data)


# ── Public API ─────────────────────────────────────────────────────────────────

def create_processing_job(
    db: Session,
    *,
    payload: ProcessingJobCreateRequest,
) -> ProcessingJobRead:
    """Create a new processing job in ``queued`` status."""
    job_id = uuid4()

    with transaction(db):
        row = db.execute(
            _INSERT_JOB,
            {
                "id": job_id,
                "asset_id": payload.asset_id,
                "pipeline_id": payload.pipeline_id,
                "target_stores": to_pg_array(payload.target_stores or []),
            },
        ).one()

    return _job_from_row(row)


def get_processing_job(db: Session, job_id: UUID) -> ProcessingJobRead | None:
    """Look up a processing job by ID."""
    row = db.execute(_SELECT_BY_ID, {"id": job_id}).first()
    if row is None:
        return None
    return _job_from_row(row)


def get_processing_job_status(db: Session, job_id: UUID) -> ProcessingJobStatus | None:
    """Look up a processing job's status for polling."""
    row = db.execute(_SELECT_BY_ID, {"id": job_id}).first()
    if row is None:
        return None
    return _status_from_row(row)


def list_processing_jobs(
    db: Session,
    *,
    asset_id: UUID | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[ProcessingJobRead], int]:
    """List processing jobs, optionally filtered by asset."""
    if asset_id:
        rows = db.execute(_SELECT_BY_ASSET, {"asset_id": asset_id}).all()
        items = [_job_from_row(r) for r in rows]
        return items, len(items)

    offset = (page - 1) * page_size
    rows = db.execute(_SELECT_ALL, {"limit": page_size, "offset": offset}).all()
    items = [_job_from_row(r) for r in rows]

    # Get total count
    total_row = db.execute(text("SELECT count(*) FROM processing_jobs")).scalar_one()
    return items, total_row


_VALID_TRANSITIONS = {
    "queued": {"processing"},
    "processing": {"done", "failed"},
    "done": set(),
    "failed": set(),
}


def advance_job_status(
    db: Session,
    *,
    job_id: UUID,
    new_status: str,
    expected_status: str | None = None,
    error: str | None = None,
    chunks_produced: int | None = None,
) -> ProcessingJobRead:
    """Atomically transition a processing job's status.

    Valid transitions:
        queued → processing
        processing → done | failed

    Uses *expected_status* for optimistic concurrency control.
    """
    if new_status not in ("queued", "processing", "done", "failed"):
        raise ValueError(f"Invalid status: {new_status}")

    with transaction(db):
        # If no expected_status given, fetch current first
        if expected_status is None:
            current = get_processing_job(db, job_id)
            if current is None:
                raise ValueError(f"Processing job {job_id} not found")
            expected_status = current.status

        # Validate transition
        valid_next = _VALID_TRANSITIONS.get(expected_status, set())
        if new_status not in valid_next:
            raise ValueError(
                f"Cannot transition from '{expected_status}' to '{new_status}'. "
                f"Valid transitions: {valid_next}"
            )

        row = db.execute(
            _TRANSITION_STATUS,
            {
                "id": job_id,
                "new_status": new_status,
                "expected_status": expected_status,
                "error": error,
                "chunks_produced": chunks_produced,
            },
        ).first()

        if row is None:
            raise ValueError(
                f"Status transition failed for job {job_id}: "
                f"expected '{expected_status}' but job was in a different state"
            )

    return _job_from_row(row)
