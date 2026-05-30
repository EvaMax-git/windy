"""Project CRUD with audit + outbox + idempotency + object registry.

Every mutation that creates or modifies a project row is wrapped in
:func:`mneme.db.audit.write_with_audit_outbox_idempotency` so that:

* A ``projects`` row is written.
* An ``object_registry`` row is written (P1-09).
* An ``object_versions`` row is written (P1-09, with ``action='create'``).
* An ``audit_events`` row is written.
* An ``events`` (outbox) row is written (``event_type = "project.created"``).
* All five land in the same database transaction.

Idempotency is enforced via ``events.idempotency_key`` UNIQUE constraint
together with a pre-write check.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, bindparam, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Session

from mneme.api.context import RequestContext
from mneme.db.compat import PG_UUID_COMPAT
from mneme.db.audit import (
    AuditEvent,
    OutboxEvent,
    write_with_audit_outbox_idempotency,
)
from mneme.domain.objects import (
    create_version,
    register_object,
)
from mneme.schemas.projects import ProjectCreateRequest, ProjectRead


_INSERT_PROJECT = text(
    """
    INSERT INTO projects (
      project_id,
      project_code,
      name,
      description,
      sensitivity_default
    )
    VALUES (
      :project_id,
      :project_code,
      :name,
      :description,
      :sensitivity_default
    )
    RETURNING
      project_id,
      project_code,
      name,
      description,
      status,
      sensitivity_default,
      created_at,
      updated_at,
      archived_at
    """
).bindparams(
    bindparam("project_id", type_=PG_UUID(as_uuid=True)),
)

_SELECT_PROJECT_BY_ID = text(
    """
    SELECT
      project_id,
      project_code,
      name,
      description,
      status,
      sensitivity_default,
      created_at,
      updated_at,
      archived_at
    FROM projects
    WHERE project_id = :project_id
    """
).bindparams(bindparam("project_id", type_=PG_UUID_COMPAT))

_SELECT_PROJECT_BY_CODE = text(
    """
    SELECT
      project_id,
      project_code,
      name,
      description,
      status,
      sensitivity_default,
      created_at,
      updated_at,
      archived_at
    FROM projects
    WHERE project_code = :project_code
    LIMIT 1
    """
)

_LIST_PROJECTS_COUNT_BY_STATUS = text(
    "SELECT count(*) FROM projects WHERE (:status IS NULL OR status = :status)"
)

_LIST_PROJECTS = text(
    """
    SELECT
      project_id,
      project_code,
      name,
      description,
      status,
      sensitivity_default,
      created_at,
      updated_at,
      archived_at
    FROM projects
    WHERE (:status IS NULL OR status = :status)
    ORDER BY created_at DESC
    LIMIT :page_size OFFSET :offset
    """
)


def _project_from_row(row: Any) -> ProjectRead:
    data = dict(row._mapping)
    return ProjectRead.model_validate(data)


def _idempotent_resolve(db: Session, project_id: UUID) -> ProjectRead:
    """Resolve an existing project by id (used in idempotent replay)."""
    row = db.execute(_SELECT_PROJECT_BY_ID, {"project_id": project_id}).first()
    if row is None:
        raise LookupError(f"project {project_id} not found during idempotent replay")
    return _project_from_row(row)


def create_project(
    db: Session,
    context: RequestContext,
    *,
    payload: ProjectCreateRequest,
) -> ProjectRead:
    """Create a project with audit, outbox event, idempotency guard, and
    object-registry registration (P1-09).

    Writes all five rows — ``projects``, ``object_registry``,
    ``object_versions``, ``audit_events``, ``events`` — inside a single
    database transaction.

    Returns the newly created :class:`ProjectRead` (or the previously created
    one if the idempotency key was already used).
    """
    project_id = uuid4()
    object_type = "project"

    outbox_event = OutboxEvent(
        event_type="project.created",
        aggregate_type=object_type,
        aggregate_id=project_id,
        aggregate_version=1,
        idempotency_key=context.idempotency_key or str(uuid4()),
        producer="mneme-api",
        payload_json={
            "project_code": payload.project_code,
            "name": payload.name,
        },
        visibility="internal",
        publish_state="pending",
    )

    audit_event = AuditEvent(
        action="project.create",
        result="success",
        object_type=object_type,
        object_id=project_id,
        sensitivity_level=payload.sensitivity_default.value,
    )

    def _do_insert(db: Session) -> ProjectRead:
        # 1. Domain row — INSERT INTO projects
        row = db.execute(
            _INSERT_PROJECT,
            {
                "project_id": project_id,
                "project_code": payload.project_code,
                "name": payload.name,
                "description": payload.description,
                "sensitivity_default": payload.sensitivity_default.value,
            },
        ).one()

        # 2. Object registry — INSERT INTO object_registry (P1-09)
        register_object(
            db,
            object_id=project_id,
            object_type=object_type,
            project_id=project_id,
            object_key=payload.project_code,
            owner_actor_type=context.actor.actor_type,
            owner_actor_id=context.actor.actor_id,
            sensitivity_level=payload.sensitivity_default.value,
        )

        return _project_from_row(row)

    def _post_audit(db: Session, audit_id: UUID, event_id: UUID) -> None:
        # 3. Object version — INSERT INTO object_versions (P1-09)
        create_version(
            db,
            object_id=project_id,
            object_type=object_type,
            version=1,
            action="create",
            actor_type=context.actor.actor_type,
            actor_id=context.actor.actor_id,
            audit_id=audit_id,
            event_id=event_id,
        )

    return write_with_audit_outbox_idempotency(
        db,
        context,
        work=_do_insert,
        audit_event=audit_event,
        outbox_event=outbox_event,
        resolve_existing=_idempotent_resolve,
        on_success=_post_audit,
    )


def get_project(db: Session, project_id: UUID) -> ProjectRead | None:
    """Look up a project by primary key."""
    row = db.execute(_SELECT_PROJECT_BY_ID, {"project_id": project_id}).first()
    if row is None:
        return None
    return _project_from_row(row)


def get_project_by_code(db: Session, project_code: str) -> ProjectRead | None:
    """Look up a project by its unique code."""
    row = db.execute(_SELECT_PROJECT_BY_CODE, {"project_code": project_code}).first()
    if row is None:
        return None
    return _project_from_row(row)


def list_projects(
    db: Session, *, page: int = 1, page_size: int = 50, status: str | None = None
) -> tuple[list[ProjectRead], int]:
    """List projects with optional status filter and pagination.

    When ``status`` is None, returns non-archived projects (backward-compatible).
    Pass ``status='archived'`` to include archived projects.
    """
    params = {"status": status, "page_size": page_size, "offset": (page - 1) * page_size}
    total = db.execute(_LIST_PROJECTS_COUNT_BY_STATUS, params).scalar_one()
    rows = db.execute(_LIST_PROJECTS, params).all()
    projects = [_project_from_row(row) for row in rows]
    return projects, total


_UPDATE_PROJECT = text(
    """
    UPDATE projects
    SET
      name = COALESCE(:name, name),
      description = CASE WHEN :description_set THEN :description ELSE description END,
      sensitivity_default = COALESCE(:sensitivity_default, sensitivity_default),
      updated_at = :now
    WHERE project_id = :project_id
      AND status != 'archived'
    RETURNING
      project_id,
      project_code,
      name,
      description,
      status,
      sensitivity_default,
      created_at,
      updated_at,
      archived_at
    """
).bindparams(
    bindparam("project_id", type_=PG_UUID(as_uuid=True)),
    bindparam("description_set", type_=Boolean()),
    bindparam("now", type_=DateTime),
)


def update_project(
    db: Session,
    *,
    project_id: UUID,
    name: str | None = None,
    description: str | None = None,
    description_set: bool = False,
    sensitivity_default: str | None = None,
) -> ProjectRead | None:
    """Update an existing non-archived project. Returns None if not found or archived."""
    row = db.execute(
        _UPDATE_PROJECT,
        {
            "project_id": project_id,
            "name": name,
            "description": description,
            "description_set": description_set,
            "sensitivity_default": sensitivity_default,
            "now": datetime.now(timezone.utc),
        },
    ).first()
    if row is None:
        return None
    return _project_from_row(row)


_ARCHIVE_PROJECT = text(
    """
    UPDATE projects
    SET
      status = 'archived',
      archived_at = :now,
      updated_at = :now
    WHERE project_id = :project_id
      AND status != 'archived'
    RETURNING
      project_id,
      project_code,
      name,
      description,
      status,
      sensitivity_default,
      created_at,
      updated_at,
      archived_at
    """
).bindparams(
    bindparam("project_id", type_=PG_UUID(as_uuid=True)),
    bindparam("now", type_=DateTime),
)


def archive_project(db: Session, *, project_id: UUID) -> ProjectRead | None:
    """Archive (soft-delete) an existing project. Returns None if not found or already archived."""
    row = db.execute(
        _ARCHIVE_PROJECT,
        {
            "project_id": project_id,
            "now": datetime.now(timezone.utc),
        },
    ).first()
    if row is None:
        return None
    return _project_from_row(row)
