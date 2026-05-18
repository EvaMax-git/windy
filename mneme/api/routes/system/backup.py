"""P2-14 / P2-16 Backup & Restore admin API.

P2-14 endpoints (already delivered):
* ``GET  /api/v4/admin/backups``              – paginated list of backups
* ``GET  /api/v4/admin/backups/{backup_id}``  – single backup detail
* ``POST /api/v4/admin/backups/{backup_id}/verify`` – verify backup integrity

P2-16 endpoints (this delivery):
* ``POST /api/v4/admin/backup``               – trigger immediate backup (async job)
* ``POST /api/v4/admin/restore``              – submit restore request (creates review_item)
* ``GET  /api/v4/admin/restore/{backup_id}/preview`` – restore preview (table/row comparison)
* ``GET  /api/v4/admin/jobs/{job_id}``        – job status / logs for backup/restore tracking
"""

from __future__ import annotations

import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query

from mneme.api.context import RequestContext, get_request_context
from mneme.api.errors import ApiError
from mneme.api.schemas import envelope
from mneme.backup.engine import (
    _default_backup_root,
    list_backups,
    run_backup,
    verify_backup,
)
from mneme.backup.manifest import BackupManifest, find_all_manifests, load_manifest
from mneme.config import get_settings
from mneme.db.audit import add_audit_event
from mneme.db.base import SessionLocal
from mneme.db.jobs import (
    add_job_log,
    create_job,
    get_job_by_id,
    get_job_logs,
    get_jobs,
    update_job_completed,
    update_job_running,
)
from mneme.db.review_items import create_review_item
from mneme.restore.preview import preview_restore as do_preview_restore
from mneme.schemas import (
    PageInfo,
    PaginationParams,
    ResponseEnvelope,
)
from mneme.schemas.backup import (
    BackupDetail,
    BackupListResponse,
    BackupSummary,
    BackupTriggerRequest,
    BackupTriggerResponse,
    BackupVerifyResponse,
    JobListResponse,
    JobLogEntry,
    JobStatusResponse,
    JobSummary,
    RestoreDetailedPreview,
    RestoreDrillRequest,
    RestoreDrillResponse,
    RestoreListResponse,
    RestoreSubmitRequest,
    RestoreSubmitResponse,
    RestoreSummary,
    TableComparisonItem,
)
from mneme.security.audit import (
    AuditEvent,
    audit_event_for_action,
)

import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin", "backup", "restore"])


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _sorted_backup_dirs() -> list[Path]:
    """Return sorted list of backup directories (newest first), or empty list if the
    backup root does not exist."""
    root = _default_backup_root()
    if not root.exists():
        return []
    return sorted(
        (e for e in root.iterdir() if e.is_dir()),
        reverse=True,
    )


def _find_manifest(backup_id: str) -> BackupManifest | None:
    """Find a backup manifest by *backup_id*."""
    for entry in _sorted_backup_dirs():
        m = load_manifest(entry)
        if m is not None and m.backup_id == backup_id:
            return m
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# P2-14: GET /admin/backups
# ═══════════════════════════════════════════════════════════════════════════════


@router.get("/backups", response_model=ResponseEnvelope[BackupListResponse])
def list_backup_manifests(
    pagination: PaginationParams = Depends(),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """列出所有可用备份，按最新优先排序。"""
    all_backups = list_backups()

    total = len(all_backups)
    page = pagination.page
    page_size = pagination.page_size
    start = (page - 1) * page_size
    end = start + page_size
    page_items = all_backups[start:end]

    items = [BackupSummary(**b) for b in page_items]

    total_pages = max(1, math.ceil(total / max(page_size, 1)))

    page_info = PageInfo(
        page=page,
        page_size=page_size,
        total_items=total,
        total_pages=total_pages,
        has_next=page < total_pages,
        has_previous=page > 1,
    )

    data = BackupListResponse(items=items, page_info=page_info)

    return envelope(
        data.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# P2-14: GET /admin/backups/{backup_id}
# ═══════════════════════════════════════════════════════════════════════════════


@router.get(
    "/backups/{backup_id}",
    response_model=ResponseEnvelope[BackupDetail],
)
def get_backup_detail(
    backup_id: str,
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """返回单个备份的完整清单详情。"""
    manifest = _find_manifest(backup_id)

    if manifest is None:
        raise ApiError(
            404,
            "bad_request",
            f"备份 '{backup_id}' 未找到",
        )

    detail = BackupDetail(
        backup_id=manifest.backup_id,
        created_at=manifest.created_at,
        pg_version=manifest.pg_version,
        format=manifest.format,
        tables=manifest.tables,
        table_row_counts=manifest.table_row_counts,
        file_path=manifest.file_path,
        file_size_bytes=manifest.file_size_bytes,
        checksum_sha256=manifest.checksum_sha256,
        alembic_revision=manifest.alembic_revision,
        status=manifest.status,
        error_message=manifest.error_message,
        completed_at=manifest.completed_at,
        dump_command=manifest.dump_command,
        env_info=manifest.env_info,
    )

    return envelope(
        detail.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# P2-14: POST /admin/backups/{backup_id}/verify
# ═══════════════════════════════════════════════════════════════════════════════


@router.post(
    "/backups/{backup_id}/verify",
    response_model=ResponseEnvelope[BackupVerifyResponse],
)
def verify_backup_integrity(
    backup_id: str,
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """验证指定备份的完整性。"""
    manifest = _find_manifest(backup_id)

    if manifest is None:
        raise ApiError(
            404,
            "bad_request",
            f"备份 '{backup_id}' 未找到",
        )

    result = verify_backup(manifest)

    checksum_match = True
    if manifest.checksum_sha256:
        from mneme.backup.manifest import verify_checksum
        dump_path = Path(manifest.file_path)
        if dump_path.exists():
            checksum_match = verify_checksum(manifest, dump_path)
        else:
            checksum_match = False
    else:
        checksum_match = None

    data = BackupVerifyResponse(
        backup_id=backup_id,
        valid=result["valid"],
        issues=result["issues"],
        file_size_bytes=manifest.file_size_bytes,
        checksum_match=checksum_match,
    )

    return envelope(
        data.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# P2-16: POST /admin/backup — trigger immediate backup (async job)
# ═══════════════════════════════════════════════════════════════════════════════


@router.post(
    "/backup",
    response_model=ResponseEnvelope[BackupTriggerResponse],
    status_code=202,
)
def trigger_backup(
    body: BackupTriggerRequest = BackupTriggerRequest(),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """触发即时备份作为异步任务。

    创建 ``jobs`` 记录以跟踪备份执行。
    实际的 ``pg_dump`` 在进程内运行（v1 中为同步），
    状态通过 jobs/job_logs 表跟踪。

    使用 ``GET /admin/jobs/{job_id}`` 跟踪进度。
    """
    backup_id = body.backup_id or str(uuid4())
    job_key = f"backup.{backup_id}"
    idempotency_key = context.idempotency_key or job_key

    # 1. Create the job record
    job = create_job(
        job_type="backup",
        job_key=job_key,
        input_payload={
            "backup_id": backup_id,
            "database_url": body.database_url,
        },
        priority=90,
        queue_name="admin",
        max_retries=1,
        timeout_seconds=3600,
        actor_type=context.actor.actor_type,
        actor_id=context.actor.actor_id,
        idempotency_key=idempotency_key,
    )

    job_id = UUID(job["job_id"])
    add_job_log(job_id, step="job.created", message=f"备份任务已入队: {backup_id}")

    # 2. Write audit event
    with SessionLocal() as db:
        add_audit_event(
            db,
            context,
            audit_event_for_action(
                action="backup.triggered",
                result="success",
                object_type="backup",
                object_id=UUID(backup_id) if _is_uuid(backup_id) else None,
                metadata_json={
                    "job_id": str(job_id),
                    "database_url": body.database_url,
                },
            ),
        )
        db.commit()

    # 3. Execute backup (v1: synchronous; future: background worker)
    _execute_backup_job(job_id=job_id, backup_id=backup_id, database_url=body.database_url)

    data = BackupTriggerResponse(
        backup_id=backup_id,
        job_id=job_id,
        status="pending",
        message="备份任务已创建并开始执行",
    )

    return envelope(
        data.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# P2-15: GET /admin/restores — list restore reports
# ═══════════════════════════════════════════════════════════════════════════════


@router.get(
    "/restores",
    response_model=ResponseEnvelope[RestoreListResponse],
)
def list_restore_reports(
    pagination: PaginationParams = Depends(),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """列出所有恢复报告，按最新优先排序。"""
    from mneme.backup.restore_engine import list_restores as _list_restores

    all_restores = _list_restores()

    total = len(all_restores)
    page = pagination.page
    page_size = pagination.page_size
    start = (page - 1) * page_size
    end = start + page_size
    page_items = all_restores[start:end]

    items = [RestoreSummary(**r) for r in page_items]

    total_pages = max(1, math.ceil(total / max(page_size, 1)))
    page_info = PageInfo(
        page=page,
        page_size=page_size,
        total_items=total,
        total_pages=total_pages,
        has_next=page < total_pages,
        has_previous=page > 1,
    )

    data = RestoreListResponse(items=items, page_info=page_info)
    return envelope(
        data.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# P2-15: POST /admin/restores/drill — execute a restore drill
# ═══════════════════════════════════════════════════════════════════════════════


@router.post(
    "/restores/drill",
    response_model=ResponseEnvelope[RestoreDrillResponse],
    status_code=202,
)
def execute_restore_drill(
    body: RestoreDrillRequest,
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """执行恢复演练：将备份恢复到临时数据库，
    验证数据完整性，生成报告，然后清理。

    演练**不会**影响生产数据库。
    """
    from mneme.backup.restore_engine import run_restore_drill

    result = run_restore_drill(
        backup_id=body.backup_id,
        target_database_url=body.target_database_url,
        keep_temp_db=body.keep_temp_db,
    )

    if result.report:
        verification = result.report.verification
        verification_summary = {
            "table_count": verification.get("table_count", {}).get("match", False),
            "row_counts": verification.get("row_counts", {}).get("match", False),
            "foreign_keys": verification.get("foreign_keys", {}).get("valid", False),
            "alembic_revision": verification.get("alembic_revision", {}).get("match", False),
        }
    else:
        verification_summary = {
            "table_count": False,
            "row_counts": False,
            "foreign_keys": False,
            "alembic_revision": False,
        }

    data = RestoreDrillResponse(
        restore_id=result.report.restore_id if result.report else "",
        success=result.success,
        status=result.report.status if result.report else "failed",
        verification_summary=verification_summary,
        report_path=str(result.output_dir) if result.output_dir else "",
        error_message=result.error_message,
    )

    return envelope(
        data.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# P2-16: POST /admin/restore — submit restore request (creates review_item)
# ═══════════════════════════════════════════════════════════════════════════════


@router.post(
    "/restore",
    response_model=ResponseEnvelope[RestoreSubmitResponse],
    status_code=201,
)
def submit_restore(
    body: RestoreSubmitRequest,
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """提交恢复请求。始终创建审核项，设置
    ``review_type='restore_confirm'`` and ``target_type='restore_run'``.

    在审核项被批准之前**不会**执行恢复操作。
    批准后，恢复操作将自动执行。

    提交前请使用 ``GET /admin/restore/{backup_id}/preview`` 预览恢复
    操作。
    """
    # 1. Verify the backup exists
    manifest = _find_manifest(str(body.backup_id))

    if manifest is None:
        raise ApiError(
            404,
            "bad_request",
            f"Backup '{body.backup_id}' not found",
        )

    # Verify backup status
    if manifest.status == "failed":
        raise ApiError(
            400,
            "bad_request",
            f"Cannot restore from a failed backup (backup_id={body.backup_id}, "
            f"error={manifest.error_message})",
        )

    # Verify dump file exists
    dump_path = Path(manifest.file_path)
    if not dump_path.exists():
        raise ApiError(
            400,
            "bad_request",
            f"转储文件未找到: {dump_path}",
        )

    # 2. Create the review item
    idempotency_key = context.idempotency_key or str(uuid4())
    target_id = uuid4()  # restore_run ID (placeholder for future pipeline_runs)

    review_row = create_review_item(
        project_id=None,
        review_type="restore_confirm",
        target_type="restore_run",
        target_id=target_id,
        status="pending",
        priority=80,  # high priority for restore operations
        requester_actor_type=context.actor.actor_type,
        requester_actor_id=context.actor.actor_id,
        due_at=None,
        expires_at=None,
        decision_payload={
            "backup_id": body.backup_id,
            "target_database_url": body.target_database_url,
            "clean": body.clean,
            "requester_note": body.reason or "",
            "backup_created_at": manifest.created_at,
            "backup_file_size_bytes": manifest.file_size_bytes,
            "backup_tables": manifest.tables,
        },
        correlation_id=context.correlation_id,
        request_id=context.request_id,
        idempotency_key=idempotency_key,
    )

    review_item_id = UUID(review_row["review_item_id"])

    # 3. Write audit event
    with SessionLocal() as db:
        add_audit_event(
            db,
            context,
            audit_event_for_action(
                action="restore.requested",
                result="success",
                object_type="restore_run",
                object_id=target_id,
                metadata_json={
                    "backup_id": body.backup_id,
                    "review_item_id": str(review_item_id),
                },
            ),
        )
        db.commit()

    data = RestoreSubmitResponse(
        backup_id=body.backup_id,
        review_item_id=review_item_id,
        status="pending",
        message="恢复请求已提交。等待审核批准。",
    )

    return envelope(
        data.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# P2-16: GET /admin/restore/{backup_id}/preview — restore preview
# ═══════════════════════════════════════════════════════════════════════════════


@router.get(
    "/restore/{backup_id}/preview",
    response_model=ResponseEnvelope[RestoreDetailedPreview],
)
def preview_restore(
    backup_id: str,
    target_database_url: str | None = Query(
        default=None,
        description="Override target database URL for comparison",
    ),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """预览恢复操作将执行的内容。

    Compares the backup manifest against the live database:
    * Per-table row count comparison
    * Identifies tables that would be created, overwritten, or dropped
    * Warnings about significant changes

    Use this before calling ``POST /admin/restore``.
    """
    preview = do_preview_restore(
        backup_id=backup_id,
        target_database_url=target_database_url,
    )

    if preview.error:
        raise ApiError(
            400 if "not found" in preview.error.lower() else 500,
            "bad_request",
            preview.error,
        )

    table_comparisons = [
        TableComparisonItem(
            table_name=tc.table_name,
            backup_rows=tc.backup_rows,
            live_rows=tc.live_rows,
            difference=tc.difference,
            exists_in_live=tc.exists_in_live,
            will_be=tc.will_be,
        )
        for tc in preview.table_comparisons
    ]

    data = RestoreDetailedPreview(
        backup_id=preview.backup_id,
        backup_created_at=preview.backup_created_at,
        backup_tables=preview.backup_tables,
        live_tables=preview.live_tables,
        table_comparisons=table_comparisons,
        total_rows_backup=preview.total_rows_backup,
        total_rows_live=preview.total_rows_live,
        will_overwrite_tables=preview.will_overwrite_tables,
        will_create_tables=preview.will_create_tables,
        will_drop_tables=preview.will_drop_tables,
        warnings=preview.warnings,
        error=preview.error,
    )

    return envelope(
        data.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# P2-19: GET /admin/jobs — list jobs with optional status filter
# ═══════════════════════════════════════════════════════════════════════════════


@router.get("/jobs", response_model=ResponseEnvelope[JobListResponse])
def list_jobs(
    pagination: PaginationParams = Depends(),
    status: str | None = Query(
        default=None,
        description="Filter by job status: pending/scheduled/running/succeeded/failed/retrying/cancelled/dead_letter",
    ),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """列出任务，支持可选状态过滤，按最新优先排序。"""
    rows, total = get_jobs(
        page=pagination.page,
        page_size=pagination.page_size,
        status=status,
    )

    items = [JobSummary(**row) for row in rows]

    total_pages = max(1, math.ceil(total / max(pagination.page_size, 1)))
    page_info = PageInfo(
        page=pagination.page,
        page_size=pagination.page_size,
        total_items=total,
        total_pages=total_pages,
        has_next=pagination.page < total_pages,
        has_previous=pagination.page > 1,
    )

    data = JobListResponse(items=items, page_info=page_info)
    return envelope(
        data.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# P2-16: GET /admin/jobs/{job_id} — job status / logs for backup/restore
# ═══════════════════════════════════════════════════════════════════════════════


@router.get(
    "/jobs/{job_id}",
    response_model=ResponseEnvelope[JobStatusResponse],
)
def get_job_status(
    job_id: UUID,
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """返回备份或恢复任务的状态和日志。"""
    job = get_job_by_id(job_id)
    if job is None:
        raise ApiError(
            404,
            "bad_request",
            f"任务 '{job_id}' 未找到",
        )

    logs = get_job_logs(job_id)
    log_entries = [JobLogEntry(**log) for log in logs]

    data = JobStatusResponse(
        job_id=UUID(job["job_id"]),
        job_type=job["job_type"],
        job_key=job["job_key"],
        status=job["status"],
        priority=job["priority"],
        queue_name=job.get("queue_name", "default"),
        scheduled_at=job["scheduled_at"],
        available_at=job.get("available_at"),
        started_at=job["started_at"],
        finished_at=job["finished_at"],
        retry_count=job["retry_count"],
        max_retries=job["max_retries"],
        input_payload=job["input"] if isinstance(job["input"], dict) else {},
        output=job["output"] if isinstance(job["output"], dict) else {},
        error=job["error"] if isinstance(job["error"], dict) else {},
        last_error=job.get("last_error"),
        logs=log_entries,
    )

    return envelope(
        data.model_dump(),
        request_id=context.request_id,
        correlation_id=context.correlation_id,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _execute_backup_job(
    *,
    job_id: UUID,
    backup_id: str,
    database_url: str | None = None,
) -> None:
    """执行实际的备份操作并更新任务记录。

    In P2-16 v1 this runs synchronously (called from the request handler).
    Future: can be moved to a background worker.
    """
    import sys as _sys

    add_job_log(job_id, step="backup.starting", message="正在启动 pg_dump")

    # Mark as running
    update_job_running(job_id)

    try:
        settings = get_settings()
        db_url = database_url or settings.database_url

        add_job_log(job_id, step="backup.dumping", message=f"正在运行 pg_dump: backup_id={backup_id}")

        result = run_backup(
            database_url=db_url,
            backup_id=backup_id,
        )

        if result.success and result.manifest:
            add_job_log(
                job_id,
                step="backup.completed",
                message=(
                    f"备份成功: {result.manifest.file_size_bytes:,} 字节, "
                    f"sha256={result.manifest.checksum_sha256[:16]}..."
                ),
            )
            update_job_completed(
                job_id,
                success=True,
                output={
                    "backup_id": result.manifest.backup_id,
                    "file_path": result.manifest.file_path,
                    "file_size_bytes": result.manifest.file_size_bytes,
                    "checksum_sha256": result.manifest.checksum_sha256[:32] + "...",
                    "tables": result.manifest.tables,
                    "created_at": result.manifest.created_at,
                },
            )
            logger.info(
                "Backup job succeeded: job_id=%s, backup_id=%s, size=%d",
                job_id,
                backup_id,
                result.manifest.file_size_bytes,
            )
        else:
            error_msg = result.error_message or "未知备份错误"
            add_job_log(
                job_id,
                step="backup.failed",
                message=error_msg,
                level="error",
            )
            update_job_completed(
                job_id,
                success=False,
                error_message=error_msg,
            )
            logger.error(
                "Backup job failed: job_id=%s, backup_id=%s, error=%s",
                job_id,
                backup_id,
                error_msg,
            )

    except Exception as exc:
        error_msg = f"备份任务异常: {exc}"
        add_job_log(
            job_id,
            step="backup.error",
            message=error_msg,
            level="error",
        )
        update_job_completed(
            job_id,
            success=False,
            error_message=error_msg,
        )
        logger.exception(
            "Backup job exception: job_id=%s, backup_id=%s",
            job_id,
            backup_id,
        )


def _is_uuid(value: str) -> bool:
    """检查字符串是否为有效的 UUID。"""
    try:
        UUID(value)
        return True
    except ValueError:
        return False
