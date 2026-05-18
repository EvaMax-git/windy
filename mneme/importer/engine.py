"""Import engine — orchestrates dry-run, preview, and formal import.

The engine ties together validators, mappers, reporter, and staging
to execute the three import modes with the correct side-effect policy:

* **dry_run** — validates only, no DB writes
* **preview** — maps fields without creating assets
* **import** — creates inbox items, optionally triggers pipeline

All modes produce structured results via the reporter module.
Formal imports create a ``pipeline_runs`` row to track execution.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from mneme.api.context import RequestContext
from mneme.importer.mappers import (
    MAPPING_REGISTRY,
    apply_transform,
    get_mapping,
)
from mneme.importer.reporter import (
    build_import_report,
    build_item_result,
    build_preview_result,
)
from mneme.importer.staging import create_inbox_for_item
from mneme.importer.validators import validate_import_payload
from mneme.schemas.importer import (
    FieldMappingSchema,
    ImportPayload,
    ImportReport,
    ImportRunMode,
    ImportSourceItem,
    ImportSourceType,
    ImportStatus,
    PreviewMapping,
    PreviewResult,
    ValidationResult,
)


class ImportEngine:
    """Orchestrates import operations (dry-run, preview, formal import).

    Usage::

        from mneme.importer.engine import ImportEngine

        engine = ImportEngine(db_session, request_context)
        report = engine.dry_run(payload)
    """

    def __init__(self, db: Session, context: RequestContext) -> None:
        self.db = db
        self.context = context

    # ── Dry Run ──────────────────────────────────────────────────────

    def dry_run(self, payload: ImportPayload) -> ValidationResult:
        """Validate import payload with zero side effects.

        No database writes occur.  Returns a structured validation result
        that callers can inspect before proceeding to a real import.

        Args:
            payload: The import payload.

        Returns:
            :class:`ValidationResult` with per-item issues.
        """
        mapping = get_mapping(payload.source_type)
        return validate_import_payload(payload, mapping=mapping)

    # ── Preview ─────────────────────────────────────────────────────

    def preview(self, payload: ImportPayload) -> PreviewResult:
        """Generate field mapping preview without creating assets.

        Shows what each source item would look like as a v4.1 asset.
        No database writes occur.

        Args:
            payload: The import payload.

        Returns:
            :class:`PreviewResult` with per-item mapping previews.

        Raises:
            ValueError: If no mapping is registered for the source type.
        """
        mapping = get_mapping(payload.source_type)
        if mapping is None:
            raise ValueError(
                f"No field mapping registered for source_type '{payload.source_type.value}'"
            )

        previews: list[PreviewMapping] = []
        for i, item in enumerate(payload.items):
            preview = self._map_single_item(i, item, mapping, payload)
            previews.append(preview)

        return build_preview_result(
            previews=previews,
            source_type=payload.source_type,
            mapping_version=mapping.version,
        )

    # ── Import ───────────────────────────────────────────────────────

    def import_(self, payload: ImportPayload) -> ImportReport:
        """Execute a formal import — creates inbox items + pipeline run.

        This is the real import with side effects:
        1. Validates the payload (raises on hard errors).
        2. Creates a ``pipeline_runs`` row to track the import.
        3. Creates an ``inbox_items`` row for each source item.
        4. Returns a structured report.

        The pipeline consumer (Phase 4+) can pick up the pipeline run
        and complete the asset creation step.

        Args:
            payload: The import payload.

        Returns:
            :class:`ImportReport` with per-item results.

        Raises:
            ValueError: If validation finds hard errors.
        """
        # 1. Validate — reject on hard errors
        mapping = get_mapping(payload.source_type)
        validation = validate_import_payload(payload, mapping=mapping)
        if not validation.passed:
            error_msgs = [
                f"[{iss.field}] {iss.message}"
                for iss in validation.issues
                if iss.severity == "error"
            ]
            raise ValueError(
                f"Import validation failed with {validation.error_count} error(s): "
                + "; ".join(error_msgs[:5])
            )

        # 2. Create pipeline run for tracking
        run_id = self._create_import_run(payload)

        # 3. Create inbox items for each source item
        started_at = datetime.now(timezone.utc)
        item_results: list[Any] = []  # ImportItemResult

        for i, item in enumerate(payload.items):
            try:
                inbox_item = create_inbox_for_item(
                    self.db,
                    self.context,
                    item=item,
                    project_id=payload.project_id,
                )
                item_results.append(
                    build_item_result(
                        index=i,
                        legacy_id=item.legacy_id,
                        # asset creation is reserved for Phase 5 pipeline
                    )
                )
            except Exception as exc:
                item_results.append(
                    build_item_result(
                        index=i,
                        legacy_id=item.legacy_id,
                        error=str(exc),
                    )
                )

        # 4. Update pipeline run with results
        failed_count = sum(1 for r in item_results if r.status == "failed")
        final_status = ImportStatus.succeeded if failed_count == 0 else ImportStatus.failed
        finished_at = datetime.now(timezone.utc)

        self._finalize_import_run(run_id, item_results, final_status, started_at, finished_at)

        return build_import_report(
            run_id=run_id,
            project_id=payload.project_id,
            source_type=payload.source_type,
            status=final_status,
            item_results=item_results,
            started_at=started_at,
            finished_at=finished_at,
        )

    # ── Internal helpers ─────────────────────────────────────────────

    def _map_single_item(
        self,
        index: int,
        item: ImportSourceItem,
        mapping: FieldMappingSchema,
        payload: ImportPayload,
    ) -> PreviewMapping:
        """Map one source item to v4.1 asset fields (preview only)."""
        target: dict[str, Any] = {}
        notes: list[str] = []
        warnings: list[str] = []

        for entry in mapping.mappings:
            legacy_value = getattr(item, entry.legacy_field, None)

            if entry.strategy == "direct_copy":
                target[entry.target_field] = legacy_value
                notes.append(f"{entry.legacy_field} → {entry.target_field} (direct)")

            elif entry.strategy == "transform":
                try:
                    transformed = apply_transform(
                        entry.transform or "identity", legacy_value, item
                    )
                    target[entry.target_field] = transformed
                    notes.append(
                        f"{entry.legacy_field} → {entry.target_field} "
                        f"(transform: {entry.transform})"
                    )
                except Exception as exc:
                    warnings.append(
                        f"Transform '{entry.transform}' failed for "
                        f"'{entry.legacy_field}': {exc}"
                    )

            elif entry.strategy == "computed":
                # Computed fields are derived from context, not legacy fields
                computed = self._compute_field(entry, item, payload)
                target[entry.target_field] = computed
                notes.append(
                    f"{entry.legacy_field} → {entry.target_field} (computed)"
                )

            elif entry.strategy == "skip":
                notes.append(f"{entry.legacy_field} → SKIPPED")

        # Add implicit fields
        target["Asset.project_id"] = str(payload.project_id)
        target["Asset.imported_from"] = "mneme2"
        target["Asset.imported_source_id"] = item.legacy_id

        return PreviewMapping(
            index=index,
            legacy_id=item.legacy_id,
            target_asset=target,
            mapping_notes=notes,
            warnings=warnings,
        )

    def _compute_field(
        self,
        entry: Any,
        item: ImportSourceItem,
        payload: ImportPayload,
    ) -> Any:
        """Compute a field value from context (no legacy field)."""
        # Placeholder — real computed fields in Phase 5
        if entry.legacy_field == "content_hash" and not item.content_hash:
            # Would compute from content_uri in Phase 5
            return None
        return None

    def _create_import_run(self, payload: ImportPayload) -> UUID:
        """Create a pipeline_runs row to track this import.

        Returns:
            The pipeline_run UUID.

        Note:
            Delegates to :func:`mneme.db.importer.create_import_run`.
            Full pipeline integration (creating a pipeline_def and
            proper pipeline_run) is reserved for Phase 5.
        """
        from mneme.db.importer import create_import_run

        return create_import_run(
            self.db,
            project_id=payload.project_id,
            source_type=payload.source_type.value,
            item_count=len(payload.items),
            idempotency_key=self.context.idempotency_key,
        )

    def _finalize_import_run(
        self,
        run_id: UUID,
        item_results: list[Any],
        status: ImportStatus,
        started_at: datetime,
        finished_at: datetime,
    ) -> None:
        """Update the pipeline run with final results.

        Delegates to :func:`mneme.db.importer.finalize_import_run`.
        """
        from mneme.db.importer import finalize_import_run

        succeeded = sum(1 for r in item_results if r.status == "succeeded")
        failed = sum(1 for r in item_results if r.status == "failed")
        skipped = sum(1 for r in item_results if r.status == "skipped")

        output = {
            "total_items": len(item_results),
            "succeeded": succeeded,
            "failed": failed,
            "skipped": skipped,
            "items": [
                {
                    "index": r.index,
                    "legacy_id": r.legacy_id,
                    "status": r.status,
                    "asset_id": str(r.asset_id) if r.asset_id else None,
                    "error": r.error,
                }
                for r in item_results
            ],
        }

        error_output = {}
        if status == ImportStatus.failed:
            errors = [r.error for r in item_results if r.error]
            error_output = {"errors": errors}

        finalize_import_run(
            self.db,
            run_id=run_id,
            status=status,
            output_json=output,
            error_json=error_output,
            started_at=started_at,
            finished_at=finished_at,
        )
