"""Importer data-access layer — import-run persistence via pipeline_runs.

Every import execution (dry-run / preview / formal import) creates a
``pipeline_runs`` row with ``trigger_type='importer'``.  This module
provides the CRUD helpers that the :class:`ImportEngine` and the API
routes share.

Design
------
* **create_import_run**   — INSERT a ``pipeline_runs`` row in ``pending`` state
* **finalize_import_run** — UPDATE the row with results (status, output, timestamps)
* **get_import_run**      — SELECT-by-PK with importer-type validation
* **list_import_runs**    — paginated listing filtered by ``trigger_type='importer'``
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Session

from mneme.schemas.importer import ImportRunRead, ImportStatus

# ═══════════════════════════════════════════════════════════════════════
# SQL statements
# ═══════════════════════════════════════════════════════════════════════

_INSERT_IMPORT_RUN = text("""
    INSERT INTO pipeline_runs (
        pipeline_run_id,
        pipeline_def_id,
        project_id,
        trigger_type,
        target_type,
        status,
        input_json,
        output_json,
        error_json,
        idempotency_key
    ) VALUES (
        :pipeline_run_id,
        :pipeline_def_id,
        :project_id,
        'importer',
        'asset',
        'pending',
        :input_json,
        :output_json,
        :error_json,
        :idempotency_key
    )
    RETURNING
        pipeline_run_id, pipeline_def_id, project_id,
        trigger_type, status,
        input_json, output_json, error_json,
        started_at, finished_at,
        created_at, updated_at
""").bindparams(
    bindparam("pipeline_run_id", type_=PG_UUID(as_uuid=True)),
    bindparam("pipeline_def_id", type_=PG_UUID(as_uuid=True)),
    bindparam("project_id", type_=PG_UUID(as_uuid=True)),
)

_UPDATE_IMPORT_RUN = text("""
    UPDATE pipeline_runs
    SET status = :status,
        started_at = :started_at,
        finished_at = :finished_at,
        output_json = :output_json,
        error_json = :error_json,
        updated_at = :updated_at
    WHERE pipeline_run_id = :pipeline_run_id
    RETURNING
        pipeline_run_id, pipeline_def_id, project_id,
        trigger_type, status,
        input_json, output_json, error_json,
        started_at, finished_at,
        created_at, updated_at
""").bindparams(
    bindparam("pipeline_run_id", type_=PG_UUID(as_uuid=True)),
)

_GET_IMPORT_RUN = text("""
    SELECT
        pipeline_run_id, pipeline_def_id, project_id,
        trigger_type, status,
        input_json, output_json, error_json,
        started_at, finished_at,
        created_at, updated_at
    FROM pipeline_runs
    WHERE pipeline_run_id = :pipeline_run_id
      AND trigger_type = 'importer'
""").bindparams(
    bindparam("pipeline_run_id", type_=PG_UUID(as_uuid=True)),
)

_LIST_IMPORT_RUNS_COUNT = text("""
    SELECT count(*)
    FROM pipeline_runs
    WHERE trigger_type = 'importer'
      AND (:status IS NULL OR status = :status)
      AND (:project_id IS NULL OR project_id = :project_id)
""").bindparams(
    bindparam("project_id", type_=PG_UUID(as_uuid=True)),
)

_LIST_IMPORT_RUNS = text("""
    SELECT
        pipeline_run_id, pipeline_def_id, project_id,
        trigger_type, status,
        input_json, output_json, error_json,
        started_at, finished_at,
        created_at, updated_at
    FROM pipeline_runs
    WHERE trigger_type = 'importer'
      AND (:status IS NULL OR status = :status)
      AND (:project_id IS NULL OR project_id = :project_id)
    ORDER BY created_at DESC
    LIMIT :page_size OFFSET :offset
""").bindparams(
    bindparam("project_id", type_=PG_UUID(as_uuid=True)),
)


# ═══════════════════════════════════════════════════════════════════════
# SQL — pipeline_defs (importer type)
# ═══════════════════════════════════════════════════════════════════════

_INSERT_IMPORTER_PIPELINE_DEF = text("""
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
        'importer',
        1,
        :name,
        :description,
        :config_json,
        'active',
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

_LOOKUP_IMPORTER_PIPELINE_DEF = text("""
    SELECT pipeline_def_id
    FROM pipeline_defs
    WHERE project_id = :project_id
      AND pipeline_type = 'importer'
      AND pipeline_code = :pipeline_code
    LIMIT 1
""").bindparams(
    bindparam("project_id", type_=PG_UUID(as_uuid=True)),
)

# ═══════════════════════════════════════════════════════════════════════
# Row → schema mapping
# ═══════════════════════════════════════════════════════════════════════


def _run_from_row(row: Any) -> ImportRunRead:
    """Convert a raw DB row to an :class:`ImportRunRead`.

    Handles SQLite's TEXT-for-JSON storage by parsing string fields
    back to dicts.  Only fields that exist on ``ImportRunRead`` are
    forwarded to Pydantic; extra DB columns (e.g. ``pipeline_def_id``,
    ``trigger_type``) are silently dropped.
    """
    data = dict(row._mapping)

    # Parse JSON fields from TEXT (SQLite compat)
    input_json_raw: dict[str, Any] = {}
    output_json_raw: dict[str, Any] = {}
    error_json_raw: dict[str, Any] = {}
    for field_name, target in (
        ("input_json", input_json_raw),
        ("output_json", output_json_raw),
        ("error_json", error_json_raw),
    ):
        val = data.get(field_name)
        if isinstance(val, str):
            try:
                target.update(json.loads(val))
            except (json.JSONDecodeError, TypeError):
                pass
        elif isinstance(val, dict):
            target.update(val)

    # Resolve status string → enum
    status_raw = data.get("status")
    if isinstance(status_raw, str) and status_raw in ImportStatus.__members__:
        status = ImportStatus(status_raw)
    elif status_raw is not None:
        try:
            status = ImportStatus(str(status_raw))
        except (ValueError, TypeError):
            status = ImportStatus.pending
    else:
        status = ImportStatus.pending

    return ImportRunRead(
        run_id=data.get("pipeline_run_id"),
        project_id=data.get("project_id"),
        status=status,
        source_type=input_json_raw.get("source_type"),
        mode=input_json_raw.get("mode", "import"),
        total_items=input_json_raw.get("item_count", 0),
        input_json=input_json_raw,
        output_json=output_json_raw,
        error_json=error_json_raw,
        started_at=data.get("started_at"),
        finished_at=data.get("finished_at"),
        created_at=data.get("created_at"),
    )


# ═══════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════


def _ensure_importer_pipeline_def(
    db: Session,
    *,
    project_id: UUID,
    created_by_user_id: UUID | None = None,
) -> UUID:
    """Find or create the default importer pipeline_def for a project.

    Each project can have one active ``pipeline_def`` with
    ``pipeline_type='importer'`` and ``pipeline_code='importer_default'``.
    If it does not exist, one is created.

    Args:
        db: Active session.
        project_id: Target project.
        created_by_user_id: User ID to record as creator (optional).

    Returns:
        The ``pipeline_def_id`` (UUID).
    """
    # Look up existing
    row = db.execute(
        _LOOKUP_IMPORTER_PIPELINE_DEF,
        {"project_id": project_id, "pipeline_code": "importer_default"},
    ).first()
    if row is not None:
        return row[0]

    # Create one
    def_id = uuid4()
    db.execute(
        _INSERT_IMPORTER_PIPELINE_DEF,
        {
            "pipeline_def_id": def_id,
            "project_id": project_id,
            "pipeline_code": "importer_default",
            "name": "Importer Default Pipeline",
            "description": "Auto-created pipeline def for import runs (trigger_type='importer').",
            "config_json": json.dumps({"steps": []}),
            "created_by_user_id": created_by_user_id or uuid4(),
        },
    )
    db.flush()
    return def_id


def create_import_run(
    db: Session,
    *,
    project_id: UUID,
    source_type: str,
    item_count: int,
    idempotency_key: str | None = None,
) -> UUID:
    """Create a ``pipeline_runs`` row to track an import execution.

    Args:
        db: Active SQLAlchemy session.
        project_id: Target project UUID.
        source_type: Source type string (e.g. ``"mneme2_item"``).
        item_count: Number of source items in the batch.
        idempotency_key: Client-supplied key to prevent duplicate runs.

    Returns:
        The ``pipeline_run_id`` (UUID) of the newly created row.

    Note:
        The row is flushed to the session but **not committed** by this
        function.  The caller owns the transaction boundary.
    """
    # Ensure an importer pipeline_def exists for this project
    pipeline_def_id = _ensure_importer_pipeline_def(db, project_id=project_id)

    run_id = uuid4()

    input_json_str = json.dumps({
        "mode": "import",
        "source_type": source_type,
        "item_count": item_count,
    })

    db.execute(
        _INSERT_IMPORT_RUN,
        {
            "pipeline_run_id": run_id,
            "pipeline_def_id": pipeline_def_id,
            "project_id": project_id,
            "input_json": input_json_str,
            "output_json": json.dumps({}),
            "error_json": json.dumps({}),
            "idempotency_key": idempotency_key or str(uuid4()),
        },
    )
    db.flush()
    return run_id


def finalize_import_run(
    db: Session,
    *,
    run_id: UUID,
    status: ImportStatus,
    output_json: dict[str, Any],
    error_json: dict[str, Any] | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
) -> ImportRunRead:
    """Update an import run with final results.

    Args:
        db: Active session.
        run_id: The pipeline_run_id to finalize.
        status: Terminal status (e.g. ``succeeded``, ``failed``).
        output_json: Structured result payload (items, counts, etc.).
        error_json: Optional error details.
        started_at: When the run started.
        finished_at: When the run completed.

    Returns:
        The updated :class:`ImportRunRead`.

    Note:
        The row is flushed but **not committed** — caller owns the
        transaction boundary.
    """
    now = datetime.now(timezone.utc)
    status_str = status.value if hasattr(status, "value") else str(status)
    row = db.execute(
        _UPDATE_IMPORT_RUN,
        {
            "pipeline_run_id": run_id,
            "status": status_str,
            "started_at": started_at,
            "finished_at": finished_at,
            "output_json": json.dumps(output_json),
            "error_json": json.dumps(error_json or {}),
            "updated_at": now,
        },
    ).first()
    db.flush()
    if row is None:
        raise LookupError(f"Import run {run_id} not found")
    return _run_from_row(row)


def get_import_run(db: Session, run_id: UUID) -> ImportRunRead | None:
    """Look up an import run by primary key.

    Only returns rows where ``trigger_type = 'importer'``.

    Args:
        db: Active session.
        run_id: Pipeline run UUID.

    Returns:
        :class:`ImportRunRead` or ``None``.
    """
    row = db.execute(
        _GET_IMPORT_RUN, {"pipeline_run_id": run_id}
    ).first()
    if row is None:
        return None
    return _run_from_row(row)


def list_import_runs(
    db: Session,
    *,
    status: str | None = None,
    project_id: UUID | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[ImportRunRead], int]:
    """Paginate import runs with optional filters.

    Args:
        db: Active session.
        status: Optional filter by status value (e.g. ``"succeeded"``).
        project_id: Optional filter by project.
        page: 1-based page number.
        page_size: Items per page (1–200).

    Returns:
        Tuple of ``(items, total_count)``.
    """
    params = {
        "status": status,
        "project_id": project_id,
    }
    total = db.execute(_LIST_IMPORT_RUNS_COUNT, params).scalar_one()
    offset = (page - 1) * page_size
    rows = db.execute(
        _LIST_IMPORT_RUNS,
        {**params, "page_size": page_size, "offset": offset},
    ).all()
    return [_run_from_row(row) for row in rows], total


def import_run_exists(db: Session, run_id: UUID) -> bool:
    """Check whether an import run exists.

    Args:
        db: Active session.
        run_id: Pipeline run UUID.

    Returns:
        ``True`` if the run exists (with ``trigger_type='importer'``).
    """
    return get_import_run(db, run_id) is not None
