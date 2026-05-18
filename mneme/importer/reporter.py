"""Import reporter — generates import reports in JSON and Markdown.

Reports summarise a completed import run with per-item status,
counts, and a human-readable markdown rendering.

Reports are stored in ``pipeline_runs.output_json`` after a successful
import, and can be retrieved via ``GET /api/v4/importer/runs/{id}``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from mneme.schemas.importer import (
    ImportItemResult,
    ImportReport,
    ImportSourceType,
    ImportStatus,
    PreviewMapping,
    PreviewResult,
)


def build_preview_result(
    previews: list[PreviewMapping],
    source_type: ImportSourceType,
    mapping_version: str = "1.0.0",
) -> PreviewResult:
    """Build a :class:`PreviewResult` from a list of preview mappings.

    Args:
        previews: List of per-item preview mappings.
        source_type: Source type used.
        mapping_version: Mapping schema version.

    Returns:
        A formatted :class:`PreviewResult`.
    """
    return PreviewResult(
        source_type=source_type,
        mapping_version=mapping_version,
        total_items=len(previews),
        previews=previews,
    )


def build_import_report(
    run_id: UUID,
    project_id: UUID,
    source_type: ImportSourceType,
    status: ImportStatus,
    item_results: list[ImportItemResult],
    *,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
) -> ImportReport:
    """Build a complete :class:`ImportReport` from import results.

    Args:
        run_id: The pipeline_run UUID tracking this import.
        project_id: Target project UUID.
        source_type: Source type imported.
        status: Overall run status.
        item_results: Per-item import results.
        started_at: When the run started.
        finished_at: When the run finished.

    Returns:
        A formatted :class:`ImportReport`.
    """
    total = len(item_results)
    succeeded = sum(1 for r in item_results if r.status == "succeeded")
    failed = sum(1 for r in item_results if r.status == "failed")
    skipped = sum(1 for r in item_results if r.status == "skipped")

    summary = (
        f"Import completed: {succeeded} succeeded, {failed} failed, "
        f"{skipped} skipped out of {total} total items."
    )

    return ImportReport(
        run_id=run_id,
        project_id=project_id,
        source_type=source_type,
        status=status,
        started_at=started_at,
        finished_at=finished_at,
        total_items=total,
        succeeded=succeeded,
        failed=failed,
        skipped=skipped,
        items=item_results,
        summary=summary,
    )


def report_to_json(report: ImportReport) -> str:
    """Serialize an import report to a JSON string.

    Args:
        report: The import report.

    Returns:
        JSON string representation.
    """
    return json.dumps(report.model_dump(mode="json"), indent=2, default=str)


def report_to_markdown(report: ImportReport) -> str:
    """Render an import report as Markdown.

    Args:
        report: The import report.

    Returns:
        Markdown string.
    """
    return report.to_markdown()


def build_item_result(
    index: int,
    legacy_id: str,
    *,
    asset_id: UUID | None = None,
    asset_uid: str | None = None,
    error: str | None = None,
    skipped: bool = False,
) -> ImportItemResult:
    """Build a single :class:`ImportItemResult`.

    Args:
        index: 0-based item index.
        legacy_id: Source legacy ID.
        asset_id: Created asset UUID (if succeeded).
        asset_uid: Created asset UID (if succeeded).
        error: Error message (if failed).
        skipped: Whether the item was skipped.

    Returns:
        A formatted :class:`ImportItemResult`.
    """
    if error:
        status = "failed"
    elif skipped:
        status = "skipped"
    else:
        status = "succeeded"

    return ImportItemResult(
        index=index,
        legacy_id=legacy_id,
        status=status,
        asset_id=asset_id,
        asset_uid=asset_uid,
        error=error,
    )
