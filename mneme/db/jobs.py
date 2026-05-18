"""P2-16 Jobs data-access layer — create and update job/log records.

Provides pure-SQL queries against the ``jobs`` and ``job_logs`` tables.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB

from mneme.db.base import SessionLocal
from mneme.api.context import RequestContext

logger = logging.getLogger(__name__)


# ── SQL templates ──────────────────────────────────────────────────────────────


_INSERT_JOB = text("""
    INSERT INTO jobs (
        job_id,
        project_id,
        job_key,
        job_type,
        status,
        priority,
        queue_name,
        scheduled_at,
        available_at,
        idempotency_key,
        max_retries,
        timeout_seconds,
        input,
        created_by_actor_type,
        created_by_actor_id,
        created_at,
        updated_at
    ) VALUES (
        :job_id,
        :project_id,
        :job_key,
        :job_type,
        :status,
        :priority,
        :queue_name,
        :scheduled_at,
        :available_at,
        :idempotency_key,
        :max_retries,
        :timeout_seconds,
        :input,
        :created_by_actor_type,
        :created_by_actor_id,
        :created_at,
        :updated_at
    )
    RETURNING job_id
""").bindparams(
    bindparam("input", type_=JSONB),
)


_UPDATE_JOB_STATUS = text("""
    UPDATE jobs
    SET status = :status,
        finished_at = :finished_at,
        output = :output,
        error = :error,
        last_error = :last_error,
        updated_at = now()
    WHERE job_id = :job_id
""").bindparams(
    bindparam("output", type_=JSONB),
    bindparam("error", type_=JSONB),
)


_UPDATE_JOB_RUNNING = text("""
    UPDATE jobs
    SET status = 'running',
        started_at = :started_at,
        updated_at = now()
    WHERE job_id = :job_id
      AND status = 'pending'
""")


_INSERT_JOB_LOG = text("""
    INSERT INTO job_logs (
        job_log_id,
        job_id,
        step,
        level,
        message,
        attempt_no,
        metadata_json,
        occurred_at
    ) VALUES (
        :job_log_id,
        :job_id,
        :step,
        :level,
        :message,
        :attempt_no,
        :metadata_json,
        :occurred_at
    )
""").bindparams(
    bindparam("metadata_json", type_=JSONB),
)


_GET_JOB_BY_ID = text("""
    SELECT
        job_id, project_id, job_key, job_type, status,
        priority, queue_name, scheduled_at, available_at,
        started_at, finished_at, lease_owner, lease_expires_at,
        idempotency_key, retry_count, max_retries, timeout_seconds,
        cause_event_id, aggregate_type, aggregate_id, target_version,
        input, output, error, last_error,
        created_by_actor_type, created_by_actor_id,
        created_at, updated_at
    FROM jobs
    WHERE job_id = :job_id
""")


_GET_JOBS_COUNT = text("""
    SELECT COUNT(*) AS total
    FROM jobs
    WHERE (:status IS NULL OR status = :status)
""")


_GET_JOBS_PAGE = text("""
    SELECT
        job_id, project_id, job_key, job_type, status,
        priority, queue_name, scheduled_at, available_at,
        started_at, finished_at, lease_owner, lease_expires_at,
        idempotency_key, retry_count, max_retries, timeout_seconds,
        cause_event_id, aggregate_type, aggregate_id, target_version,
        input, output, error, last_error,
        created_by_actor_type, created_by_actor_id,
        created_at, updated_at
    FROM jobs
    WHERE (:status IS NULL OR status = :status)
    ORDER BY created_at DESC
    LIMIT :limit OFFSET :offset
""")


_GET_JOB_LOGS = text("""
    SELECT
        job_log_id, job_id, step, level, message,
        attempt_no, event_id, metadata_json, occurred_at
    FROM job_logs
    WHERE job_id = :job_id
    ORDER BY occurred_at ASC
""")


# ── Public API ──────────────────────────────────────────────────────────────────


def create_job(
    *,
    job_type: str,
    job_key: str,
    input_payload: dict[str, Any] | None = None,
    project_id: UUID | None = None,
    priority: int = 100,
    queue_name: str = "default",
    max_retries: int = 0,
    timeout_seconds: int = 3600,
    actor_type: str = "system",
    actor_id: UUID | None = None,
    idempotency_key: str | None = None,
) -> dict:
    """Create a new job record.

    Returns the inserted row as a dict.
    """
    job_id = uuid4()
    now = datetime.now(timezone.utc)

    params = {
        "job_id": job_id,
        "project_id": project_id,
        "job_key": job_key,
        "job_type": job_type,
        "status": "pending",
        "priority": priority,
        "queue_name": queue_name,
        "scheduled_at": now,
        "available_at": now,
        "idempotency_key": idempotency_key or str(uuid4()),
        "max_retries": max_retries,
        "timeout_seconds": timeout_seconds,
        "input": input_payload or {},
        "created_by_actor_type": actor_type,
        "created_by_actor_id": actor_id,
        "created_at": now,
        "updated_at": now,
    }

    with SessionLocal() as db:
        db.execute(_INSERT_JOB, params)
        db.commit()

    return get_job_by_id(job_id)


def get_job_by_id(job_id: UUID) -> dict | None:
    """Return a single job row by primary key, or None."""
    with SessionLocal() as db:
        row = db.execute(_GET_JOB_BY_ID, {"job_id": job_id}).mappings().first()
        if row is None:
            return None
        return _job_row_to_dict(row)


def get_jobs(
    *,
    page: int = 1,
    page_size: int = 50,
    status: str | None = None,
) -> tuple[list[dict], int]:
    """Return paginated job list with optional status filter.

    Returns (rows, total).
    """
    page = max(1, page)
    page_size = min(max(1, page_size), 200)
    offset = (page - 1) * page_size

    with SessionLocal() as db:
        total_row = db.execute(
            _GET_JOBS_COUNT, {"status": status}
        ).mappings().first()
        total = total_row["total"] if total_row else 0

        rows = db.execute(
            _GET_JOBS_PAGE,
            {"status": status, "limit": page_size, "offset": offset},
        ).mappings().all()

        return [_job_row_to_dict(row) for row in rows], total


def update_job_running(job_id: UUID) -> bool:
    """Mark a job as running. Returns True if updated."""
    with SessionLocal() as db:
        result = db.execute(
            _UPDATE_JOB_RUNNING,
            {"job_id": job_id, "started_at": datetime.now(timezone.utc)},
        )
        db.commit()
        return result.rowcount > 0


def update_job_completed(
    job_id: UUID,
    *,
    success: bool,
    output: dict[str, Any] | None = None,
    error_message: str | None = None,
) -> bool:
    """Mark a job as succeeded or failed. Returns True if updated."""
    status = "succeeded" if success else "failed"
    now = datetime.now(timezone.utc)

    params = {
        "job_id": job_id,
        "status": status,
        "finished_at": now,
        "output": output or {},
        "error": {"message": error_message} if error_message else {},
        "last_error": error_message[:500] if error_message else None,
    }

    with SessionLocal() as db:
        result = db.execute(_UPDATE_JOB_STATUS, params)
        db.commit()
        return result.rowcount > 0


def add_job_log(
    job_id: UUID,
    *,
    step: str,
    message: str,
    level: str = "info",
    attempt_no: int = 0,
    metadata: dict[str, Any] | None = None,
) -> UUID:
    """Add a log entry for a job.

    Returns the job_log_id.
    """
    log_id = uuid4()

    params = {
        "job_log_id": log_id,
        "job_id": job_id,
        "step": step,
        "level": level,
        "message": message[:1000] if message else "",
        "attempt_no": attempt_no,
        "metadata_json": metadata or {},
        "occurred_at": datetime.now(timezone.utc),
    }

    with SessionLocal() as db:
        db.execute(_INSERT_JOB_LOG, params)
        db.commit()

    return log_id


def get_job_logs(job_id: UUID) -> list[dict]:
    """Return all log entries for a job, ordered by time."""
    with SessionLocal() as db:
        rows = db.execute(_GET_JOB_LOGS, {"job_id": job_id}).mappings().all()
        return [_log_row_to_dict(row) for row in rows]


# ── Internal helpers ────────────────────────────────────────────────────────────


def _job_row_to_dict(row) -> dict:
    """Convert a SQLAlchemy RowMapping to a plain dict safe for JSON."""
    return {
        "job_id": str(row["job_id"]),
        "project_id": str(row["project_id"]) if row.get("project_id") else None,
        "job_key": row["job_key"],
        "job_type": row["job_type"],
        "status": row["status"],
        "priority": row["priority"],
        "queue_name": row["queue_name"],
        "scheduled_at": _iso(row.get("scheduled_at")),
        "available_at": _iso(row.get("available_at")),
        "started_at": _iso(row.get("started_at")),
        "finished_at": _iso(row.get("finished_at")),
        "lease_owner": row.get("lease_owner"),
        "lease_expires_at": _iso(row.get("lease_expires_at")),
        "idempotency_key": row["idempotency_key"],
        "retry_count": row["retry_count"],
        "max_retries": row["max_retries"],
        "timeout_seconds": row["timeout_seconds"],
        "cause_event_id": str(row["cause_event_id"]) if row.get("cause_event_id") else None,
        "aggregate_type": row.get("aggregate_type"),
        "aggregate_id": str(row["aggregate_id"]) if row.get("aggregate_id") else None,
        "target_version": row.get("target_version"),
        "input": row.get("input") or {},
        "output": row.get("output") or {},
        "error": row.get("error") or {},
        "last_error": row.get("last_error"),
        "created_by_actor_type": row["created_by_actor_type"],
        "created_by_actor_id": str(row["created_by_actor_id"]) if row.get("created_by_actor_id") else None,
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
    }


def _log_row_to_dict(row) -> dict:
    return {
        "job_log_id": str(row["job_log_id"]),
        "job_id": str(row["job_id"]),
        "step": row["step"],
        "level": row["level"],
        "message": row["message"],
        "attempt_no": row["attempt_no"],
        "event_id": str(row["event_id"]) if row.get("event_id") else None,
        "metadata_json": row.get("metadata_json") or {},
        "occurred_at": _iso(row.get("occurred_at")),
    }


def _iso(dt) -> str | None:
    if dt is None:
        return None
    return dt.isoformat()
