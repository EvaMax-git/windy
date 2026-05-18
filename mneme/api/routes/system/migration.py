"""P4-14 Migration Admin API — database schema migration management.

Endpoints
---------
* ``GET    /api/v4/admin/migrations``           — list all migration revisions with status
* ``GET    /api/v4/admin/migrations/state``     — current migration state summary
* ``GET    /api/v4/admin/migrations/{rev_id}``  — single revision detail
* ``POST   /api/v4/admin/migrations/apply``     — apply pending migrations (upgrade)
* ``POST   /api/v4/admin/migrations/rollback``  — rollback applied migrations (downgrade)
* ``POST   /api/v4/admin/migrations/preview``   — preview pending migrations (dry-run)
* ``GET    /api/v4/admin/migrations/runs``      — list migration run history
* ``GET    /api/v4/admin/migrations/runs/{run_id}`` — single migration run detail
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from mneme.api.context import RequestContext, get_request_context
from mneme.api.errors import ApiError
from mneme.api.schemas import envelope
from mneme.db.audit import add_audit_event
from mneme.db.base import SessionLocal, get_db
from mneme.schemas import (
    PageInfo,
    PaginationParams,
    ResponseEnvelope,
)
from mneme.schemas.migration import (
    MigrationApplyRequest,
    MigrationApplyResponse,
    MigrationDirection,
    MigrationFilterParams,
    MigrationPreviewResponse,
    MigrationRevisionListResponse,
    MigrationRevisionRead,
    MigrationRollbackRequest,
    MigrationRunFilterParams,
    MigrationRunListResponse,
    MigrationRunRead,
    MigrationStateSummary,
    MigrationStatus,
)
from mneme.security.audit import (
    audit_event_for_action,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/migrations", tags=["admin", "migration"])


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _find_project_root() -> Path:
    """Walk up from this file's directory to find the project root.

    The project root is the first ancestor that contains ``db/alembic/alembic.ini``
    (or, alternately, ``pyproject.toml``).
    """
    from pathlib import Path

    here = Path(__file__).resolve().parent
    for ancestor in [here] + list(here.parents):
        alembic_ini = ancestor / "db" / "alembic" / "alembic.ini"
        if alembic_ini.is_file():
            return ancestor
        if (ancestor / "pyproject.toml").is_file():
            # Accept as root even without alembic (fallback)
            return ancestor
    # Ultimate fallback: go up 4 levels (heuristic)
    return Path(__file__).resolve().parent.parent.parent.parent


def _get_alembic_config():
    """Return the Alembic configuration for the project."""
    from alembic.config import Config

    project_root = _find_project_root()
    alembic_dir = project_root / "db" / "alembic"
    alembic_cfg = Config(str(alembic_dir / "alembic.ini"))
    # Override script_location to use absolute path
    alembic_cfg.set_main_option("script_location", str(alembic_dir))
    return alembic_cfg


def _get_all_revisions(alembic_cfg) -> list[dict]:
    """Scan alembic versions directory and return all known revisions."""
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(alembic_cfg)
    revisions = []
    for rev in script.walk_revisions(base="base", head="heads"):
        revisions.append({
            "revision_id": rev.revision,
            "down_revision": rev.down_revision,
            "message": rev.doc,
            "file_name": rev.module if isinstance(rev.module, str) else getattr(rev.module, "__name__", str(rev.module)),
        })
    return revisions


def _get_applied_revisions(db: Session) -> dict[str, dict]:
    """Query alembic_version table for applied revisions.

    The standard Alembic ``alembic_version`` table only has a
    ``version_num`` column (no ``applied_at``).  This function is
    intentionally conservative — if the table does not exist or is
    malformed we return an empty dict (all revisions appear pending).
    """
    from sqlalchemy import text

    try:
        result = db.execute(text("SELECT version_num FROM alembic_version"))
        rows = result.fetchall()
        return {
            row[0]: {
                "applied_at": None,
                "status": "applied",
            }
            for row in rows
        }
    except Exception:
        # alembic_version table may not exist yet
        return {}


def _get_current_head(alembic_cfg) -> str | None:
    """Return the current head revision ID."""
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(alembic_cfg)
    heads = script.get_heads()
    return heads[0] if heads else None


def _page_info(total: int, page: int, page_size: int) -> PageInfo:
    total_pages = max(1, math.ceil(total / max(page_size, 1)))
    return PageInfo(
        page=page,
        page_size=page_size,
        total_items=total,
        total_pages=total_pages,
        has_next=page < total_pages,
        has_previous=page > 1,
    )


def _enrich_revisions(
    alembic_cfg,
    db: Session,
    search: str | None = None,
    status_filter: MigrationStatus | None = None,
) -> list[MigrationRevisionRead]:
    """Build the full list of MigrationRevisionRead by merging script + DB info."""
    all_revs = _get_all_revisions(alembic_cfg)
    applied_map = _get_applied_revisions(db)

    enriched = []
    for rev in all_revs:
        applied = applied_map.get(rev["revision_id"])
        if applied:
            rev_status = MigrationStatus.applied
            applied_at = applied.get("applied_at")
            applied_by = applied.get("applied_by")
        else:
            rev_status = MigrationStatus.pending
            applied_at = None
            applied_by = None

        enriched.append(MigrationRevisionRead(
            revision_id=rev["revision_id"],
            down_revision=rev.get("down_revision"),
            message=rev.get("message"),
            status=rev_status,
            applied_at=applied_at,
            applied_by=applied_by,
            file_name=rev.get("file_name"),
        ))

    # Apply filters
    if search:
        search_lower = search.lower()
        enriched = [
            r for r in enriched
            if search_lower in r.revision_id.lower() or (r.message and search_lower in r.message.lower())
        ]
    if status_filter:
        enriched = [r for r in enriched if r.status == status_filter]

    return enriched


# ═══════════════════════════════════════════════════════════════════════════════
# GET /admin/migrations — list all migration revisions with status
# ═══════════════════════════════════════════════════════════════════════════════


@router.get("", response_model=ResponseEnvelope[MigrationRevisionListResponse])
def list_migrations(
    pagination: PaginationParams = Depends(),
    filters: MigrationFilterParams = Depends(),
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """列出所有迁移修订版本及其状态（已应用/待处理/失败等）。

    合并 Alembic 脚本目录中的信息与数据库中的 ``alembic_version`` 表信息。
    """
    alembic_cfg = _get_alembic_config()
    all_enriched = _enrich_revisions(
        alembic_cfg,
        db,
        search=filters.search,
        status_filter=filters.status,
    )

    total = len(all_enriched)
    page = pagination.page
    page_size = pagination.page_size
    start = (page - 1) * page_size
    end = start + page_size
    page_items = all_enriched[start:end]

    data = MigrationRevisionListResponse(
        items=page_items,
        page_info=_page_info(total, page, page_size),
    )

    return envelope(
        data.model_dump(mode="json"),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# GET /admin/migrations/state — current migration state summary
# ═══════════════════════════════════════════════════════════════════════════════


@router.get("/state", response_model=ResponseEnvelope[MigrationStateSummary])
def migration_state(
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """返回当前迁移状态的摘要：当前 head、已应用计数、待处理计数等。

    这是检查数据库是否与最新迁移保持同步的首选端点。
    """
    alembic_cfg = _get_alembic_config()
    all_enriched = _enrich_revisions(alembic_cfg, db)
    head = _get_current_head(alembic_cfg)

    applied_count = sum(1 for r in all_enriched if r.status == MigrationStatus.applied)
    pending_count = sum(1 for r in all_enriched if r.status == MigrationStatus.pending)
    failed_count = sum(1 for r in all_enriched if r.status == MigrationStatus.failed)
    latest_applied = max(
        (r for r in all_enriched if r.status == MigrationStatus.applied),
        key=lambda r: r.applied_at or datetime.min.replace(tzinfo=timezone.utc),
        default=None,
    )

    # Latest available revision (from script directory, typically "head")
    latest_info = None
    if all_enriched:
        # Find the revision whose down_revision is not referenced by any
        # other rev's revision_id as down_revision.
        # down_revision can be a str (single parent) or a list[str] (merge).
        rev_ids = {r.revision_id for r in all_enriched}
        candidates = []
        for r in all_enriched:
            dr = r.down_revision
            if dr is None:
                continue
            if isinstance(dr, list):
                # Merge revision: all parents must be present
                if all(p in rev_ids for p in dr):
                    continue
            else:
                # Single parent
                if dr in rev_ids:
                    continue
            candidates.append(r)
        # Fallback to last in list
        head_rev = candidates[-1] if candidates else all_enriched[-1]
        latest_info = head_rev

    data = MigrationStateSummary(
        current_head=head,
        latest_applied=latest_applied.revision_id if latest_applied else None,
        total_revisions=len(all_enriched),
        applied_count=applied_count,
        pending_count=pending_count,
        failed_count=failed_count,
        is_up_to_date=latest_applied is not None and latest_applied.revision_id == head,
        latest_revision_info=latest_info,
    )

    return envelope(
        data.model_dump(mode="json"),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# GET /admin/migrations/{revision_id} — single revision detail
# ═══════════════════════════════════════════════════════════════════════════════


@router.get("/{revision_id}", response_model=ResponseEnvelope[MigrationRevisionRead])
def get_migration_revision(
    revision_id: str,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """获取单个迁移修订版本的详细信息。"""
    alembic_cfg = _get_alembic_config()
    all_enriched = _enrich_revisions(alembic_cfg, db)

    # Match by prefix or exact
    matched = [r for r in all_enriched if r.revision_id.startswith(revision_id)]
    if not matched:
        raise ApiError(
            404,
            "bad_request",
            f"迁移修订版本未找到: {revision_id}",
        )
    # Prefer exact match
    exact = [r for r in matched if r.revision_id == revision_id]
    rev = exact[0] if exact else matched[0]

    return envelope(
        rev.model_dump(mode="json"),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# POST /admin/migrations/preview — preview pending migrations (dry-run)
# ═══════════════════════════════════════════════════════════════════════════════


@router.post("/preview", response_model=ResponseEnvelope[MigrationPreviewResponse])
def preview_migrations(
    body: MigrationApplyRequest = MigrationApplyRequest(),
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """预览哪些迁移将被应用（或回滚），不执行任何操作。

    返回将运行的迁移列表，以及任何警告。
    """
    from alembic.script import ScriptDirectory
    from alembic.runtime.migration import MigrationContext

    alembic_cfg = _get_alembic_config()
    script = ScriptDirectory.from_config(alembic_cfg)
    applied_map = _get_applied_revisions(db)

    # Determine current and target revisions
    applied_ids = list(applied_map.keys())
    all_head = _get_current_head(alembic_cfg)

    from_rev = applied_ids[-1] if applied_ids else "base"
    to_rev = body.target_revision or all_head

    # Get the revision list
    revisions_to_apply = []
    warnings = []

    try:
        revs = script.get_revisions(to_rev)
        # Walk from current to target
        for rev in script.iterate_revisions(to_rev, from_rev):
            revisions_to_apply.append(rev.revision)
    except Exception as e:
        warnings.append(f"无法解析修订范围: {e}")

    if not revisions_to_apply:
        warnings.append("没有待处理的迁移需要应用。数据库已是最新状态。")

    data = MigrationPreviewResponse(
        direction=MigrationDirection.upgrade,
        from_revision=from_rev,
        to_revision=to_rev or "head",
        revisions_to_apply=revisions_to_apply,
        sql_statements=[],
        total_steps=len(revisions_to_apply),
        warnings=warnings,
    )

    return envelope(
        data.model_dump(mode="json"),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# POST /admin/migrations/apply — apply pending migrations (upgrade)
# ═══════════════════════════════════════════════════════════════════════════════


@router.post("/apply", response_model=ResponseEnvelope[MigrationApplyResponse], status_code=200)
def apply_migrations(
    body: MigrationApplyRequest = MigrationApplyRequest(),
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """应用待处理的迁移（升级）。

    如果 ``dry_run=True``，则仅预览，不执行任何操作。
    如果 ``sql_only=True``，则将生成的 SQL 作为预览返回，而不执行。
    """
    from alembic import command
    from alembic.script import ScriptDirectory

    alembic_cfg = _get_alembic_config()
    run_id = uuid4()
    target = body.target_revision or "head"

    # Determine pending revisions before migration
    script = ScriptDirectory.from_config(alembic_cfg)
    applied_before = set(_get_applied_revisions(db).keys())

    revisions_applied: list[str] = []
    revisions_failed: list[str] = []
    error_message: str | None = None

    if body.dry_run or body.sql_only:
        # Dry-run / SQL-only mode: just preview
        from_rev = list(applied_before)[-1] if applied_before else "base"
        pending = []
        try:
            for rev in script.iterate_revisions(target, from_rev):
                pending.append(rev.revision)
        except Exception as e:
            error_message = str(e)

        sql_statements: list[str] = []
        if body.sql_only:
            # Generate SQL using offline mode
            from io import StringIO
            buf = StringIO()
            try:
                command.upgrade(alembic_cfg, target, sql=True, output_buffer=buf)
                sql_statements = [s for s in buf.getvalue().split("\n") if s.strip()]
            except Exception as e:
                error_message = str(e)

        data = MigrationApplyResponse(
            run_id=run_id,
            direction=MigrationDirection.upgrade,
            status=MigrationStatus.pending if not error_message else MigrationStatus.failed,
            revisions_applied=pending if not error_message else [],
            revisions_failed=pending if error_message else [],
            message="预览完成" if not error_message else "预览失败",
            error_message=error_message,
        )

        return envelope(
            data.model_dump(mode="json"),
            request_id=context.request_id,
            correlation_id=context.correlation_id,
        )

    # Actual migration execution
    status = MigrationStatus.applied
    message = "迁移应用完成"
    error = None

    try:
        with SessionLocal() as audit_db:
            add_audit_event(
                audit_db,
                context,
                audit_event_for_action(
                    action="migration.apply.started",
                    result="success",
                    object_type="migration",
                    object_id=run_id,
                    metadata_json={
                        "target_revision": target,
                        "run_id": str(run_id),
                    },
                ),
            )
            audit_db.commit()

        # Execute alembic upgrade
        command.upgrade(alembic_cfg, target)

        # Determine which ones were actually applied
        applied_after = set(_get_applied_revisions(db).keys())
        revisions_applied = list(applied_after - applied_before)

        with SessionLocal() as audit_db:
            add_audit_event(
                audit_db,
                context,
                audit_event_for_action(
                    action="migration.apply.completed",
                    result="success",
                    object_type="migration",
                    object_id=run_id,
                    metadata_json={
                        "run_id": str(run_id),
                        "revisions_applied": revisions_applied,
                    },
                ),
            )
            audit_db.commit()

    except Exception as e:
        logger.exception("Migration apply failed: run_id=%s", run_id)
        error = str(e)
        status = MigrationStatus.failed
        message = "迁移应用失败"

        with SessionLocal() as audit_db:
            add_audit_event(
                audit_db,
                context,
                audit_event_for_action(
                    action="migration.apply.failed",
                    result="failure",
                    object_type="migration",
                    object_id=run_id,
                    metadata_json={
                        "run_id": str(run_id),
                        "error": error,
                    },
                ),
            )
            audit_db.commit()

    data = MigrationApplyResponse(
        run_id=run_id,
        direction=MigrationDirection.upgrade,
        status=status,
        revisions_applied=revisions_applied,
        revisions_failed=revisions_failed,
        message=message,
        error_message=error,
    )

    return envelope(
        data.model_dump(mode="json"),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# POST /admin/migrations/rollback — rollback applied migrations (downgrade)
# ═══════════════════════════════════════════════════════════════════════════════


@router.post("/rollback", response_model=ResponseEnvelope[MigrationApplyResponse], status_code=200)
def rollback_migrations(
    body: MigrationRollbackRequest,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """回滚已应用的迁移（降级）。

    回滚到指定的目标修订版本。
    """
    from alembic import command
    from alembic.script import ScriptDirectory

    alembic_cfg = _get_alembic_config()
    run_id = uuid4()

    applied_before = set(_get_applied_revisions(db).keys())
    revisions_applied: list[str] = []
    revisions_failed: list[str] = []
    error_message: str | None = None

    if body.dry_run:
        # Dry-run mode: just preview
        script = ScriptDirectory.from_config(alembic_cfg)
        from_rev = list(applied_before)[-1] if applied_before else "head"
        pending = []
        try:
            for rev in script.iterate_revisions(from_rev, body.target_revision):
                pending.append(rev.revision)
        except Exception as e:
            error_message = str(e)

        data = MigrationApplyResponse(
            run_id=run_id,
            direction=MigrationDirection.downgrade,
            status=MigrationStatus.pending if not error_message else MigrationStatus.failed,
            revisions_applied=pending if not error_message else [],
            revisions_failed=pending if error_message else [],
            message="回滚预览完成" if not error_message else "预览失败",
            error_message=error_message,
        )

        return envelope(
            data.model_dump(mode="json"),
            request_id=context.request_id,
            correlation_id=context.correlation_id,
        )

    # Actual rollback execution
    status = MigrationStatus.applied
    message = "迁移回滚完成"
    error = None

    try:
        with SessionLocal() as audit_db:
            add_audit_event(
                audit_db,
                context,
                audit_event_for_action(
                    action="migration.rollback.started",
                    result="success",
                    object_type="migration",
                    object_id=run_id,
                    metadata_json={
                        "target_revision": body.target_revision,
                        "run_id": str(run_id),
                    },
                ),
            )
            audit_db.commit()

        # Execute alembic downgrade
        command.downgrade(alembic_cfg, body.target_revision)

        # Determine which ones were rolled back
        applied_after = set(_get_applied_revisions(db).keys())
        revisions_applied = list(applied_before - applied_after)

        with SessionLocal() as audit_db:
            add_audit_event(
                audit_db,
                context,
                audit_event_for_action(
                    action="migration.rollback.completed",
                    result="success",
                    object_type="migration",
                    object_id=run_id,
                    metadata_json={
                        "run_id": str(run_id),
                        "revisions_rolled_back": revisions_applied,
                    },
                ),
            )
            audit_db.commit()

    except Exception as e:
        logger.exception("Migration rollback failed: run_id=%s", run_id)
        error = str(e)
        status = MigrationStatus.failed
        message = "迁移回滚失败"

        with SessionLocal() as audit_db:
            add_audit_event(
                audit_db,
                context,
                audit_event_for_action(
                    action="migration.rollback.failed",
                    result="failure",
                    object_type="migration",
                    object_id=run_id,
                    metadata_json={
                        "run_id": str(run_id),
                        "error": error,
                    },
                ),
            )
            audit_db.commit()

    data = MigrationApplyResponse(
        run_id=run_id,
        direction=MigrationDirection.downgrade,
        status=status,
        revisions_applied=revisions_applied,
        revisions_failed=revisions_failed,
        message=message,
        error_message=error,
    )

    return envelope(
        data.model_dump(mode="json"),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# GET /admin/migrations/runs — list migration run history
# ═══════════════════════════════════════════════════════════════════════════════


@router.get("/runs", response_model=ResponseEnvelope[MigrationRunListResponse])
def list_migration_runs(
    pagination: PaginationParams = Depends(),
    filters: MigrationRunFilterParams = Depends(),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """列出迁移运行历史记录。

    从审计日志中查询迁移运行记录。
    """
    # Query audit events for migration runs
    with SessionLocal() as db:
        from sqlalchemy import text

        where_clauses = ["ae.event_action LIKE :action_prefix"]
        params = {"action_prefix": "migration.%"}

        if filters.status:
            where_clauses.append("ae.event_result = :result")
            params["result"] = "success" if filters.status == MigrationStatus.applied else "failure"

        where_sql = " AND ".join(where_clauses)

        count_sql = f"SELECT COUNT(*) FROM audit_events ae WHERE {where_sql}"
        total = db.execute(text(count_sql), params).scalar() or 0

        query_sql = f"""
            SELECT ae.event_id, ae.event_action, ae.event_result,
                   ae.event_metadata, ae.created_at
            FROM audit_events ae
            WHERE {where_sql}
            ORDER BY ae.created_at DESC
            LIMIT :limit OFFSET :offset
        """
        params["limit"] = pagination.page_size
        params["offset"] = (pagination.page - 1) * pagination.page_size

        rows = db.execute(text(query_sql), params).fetchall()

        items = []
        for row in rows:
            metadata = row[3] or {}
            if isinstance(metadata, str):
                import json
                try:
                    metadata = json.loads(metadata)
                except json.JSONDecodeError:
                    metadata = {}

            run_id = metadata.get("run_id", str(row[0]))
            direction = MigrationDirection.downgrade if "rollback" in (row[1] or "") else MigrationDirection.upgrade

            try:
                items.append(MigrationRunRead(
                    run_id=UUID(run_id),
                    direction=direction,
                    target_revision=metadata.get("target_revision"),
                    status=MigrationStatus.applied if row[2] == "success" else MigrationStatus.failed,
                    revisions_applied=metadata.get("revisions_applied") or metadata.get("revisions_rolled_back") or [],
                    revisions_failed=metadata.get("revisions_failed") or [],
                    error_message=metadata.get("error"),
                    started_at=row[4],
                    created_at=row[4],
                    triggered_by=metadata.get("triggered_by"),
                ))
            except ValueError:
                continue

        data = MigrationRunListResponse(
            items=items,
            page_info=_page_info(total, pagination.page, pagination.page_size),
        )

    return envelope(
        data.model_dump(mode="json"),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# GET /admin/migrations/runs/{run_id} — single migration run detail
# ═══════════════════════════════════════════════════════════════════════════════


@router.get("/runs/{run_id}", response_model=ResponseEnvelope[MigrationRunRead])
def get_migration_run(
    run_id: UUID,
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """获取单个迁移运行的详细信息。"""
    with SessionLocal() as db:
        from sqlalchemy import text

        # Look through audit events for this run_id
        query = text("""
            SELECT ae.event_id, ae.event_action, ae.event_result,
                   ae.event_metadata, ae.created_at
            FROM audit_events ae
            WHERE ae.event_action LIKE :action_prefix
            ORDER BY ae.created_at DESC
        """)

        rows = db.execute(query, {"action_prefix": "migration.%"}).fetchall()

        found = None
        for row in rows:
            metadata = row[3] or {}
            if isinstance(metadata, str):
                import json
                try:
                    metadata = json.loads(metadata)
                except json.JSONDecodeError:
                    metadata = {}
            if str(metadata.get("run_id")) == str(run_id):
                direction = MigrationDirection.downgrade if "rollback" in (row[1] or "") else MigrationDirection.upgrade
                found = MigrationRunRead(
                    run_id=run_id,
                    direction=direction,
                    target_revision=metadata.get("target_revision"),
                    status=MigrationStatus.applied if row[2] == "success" else MigrationStatus.failed,
                    revisions_applied=metadata.get("revisions_applied") or metadata.get("revisions_rolled_back") or [],
                    revisions_failed=metadata.get("revisions_failed") or [],
                    error_message=metadata.get("error"),
                    started_at=row[4],
                    created_at=row[4],
                    triggered_by=metadata.get("triggered_by"),
                )
                break

        if found is None:
            raise ApiError(
                404,
                "bad_request",
                f"迁移运行记录未找到: {run_id}",
            )

    return envelope(
        found.model_dump(mode="json"),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )
