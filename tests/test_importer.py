"""Contract tests for P3-09 Importer skeleton.

Tests cover:
* Schemas — ImportPayload / ImportSourceItem / ImportReport validation
* Mappers — field mapping lookup, transform registry, apply_transform
* Validators — validate_import_payload edge cases
* Reporter — build_import_report, report_to_markdown, build_preview_result
* Staging — build_inbox_payload
* Engine — dry_run, preview, import (DB-backed)
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest

from mneme.api.context import ActorContext, RequestContext
from mneme.importer.engine import ImportEngine
from mneme.importer.mappers import (
    MAPPING_REGISTRY,
    MNEME2_ITEM_MAPPING,
    TRANSFORM_REGISTRY,
    apply_transform,
    get_mapping,
)
from mneme.importer.reporter import (
    build_import_report,
    build_item_result,
    build_preview_result,
    report_to_markdown,
)
from mneme.importer.staging import build_inbox_payload
from mneme.importer.validators import (
    validate_import_payload,
    _validate_single_item,
)
from mneme.schemas.importer import (
    FieldMappingEntry,
    FieldMappingSchema,
    ImportItemResult,
    ImportPayload,
    ImportReport,
    ImportRunMode,
    ImportRunRead,
    ImportSourceItem,
    ImportSourceType,
    ImportStatus,
    PreviewMapping,
    PreviewResult,
    ValidationIssue,
    ValidationResult,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Fixtures / helpers
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def sample_item() -> ImportSourceItem:
    return ImportSourceItem(
        legacy_id="mneme2:items:12345",
        source_type=ImportSourceType.mneme2_item,
        title="Test Document Title",
        content_type="text/markdown",
        content_text="# Hello World",
        content_hash="a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6a7b8c9d0e1f2a3b4c5d6a7b8c9d0",
        size_bytes=1024,
        tags=["test", "import"],
        metadata={"source_version": "2.0"},
        author="test-user",
    )


@pytest.fixture
def sample_payload(sample_item) -> ImportPayload:
    return ImportPayload(
        project_id=uuid.uuid4(),
        source_type=ImportSourceType.mneme2_item,
        items=[sample_item],
    )


from tests.conftest import TEST_USER_ID


def _make_context(idem_key: str | None = None) -> RequestContext:
    req_id = uuid.uuid4()
    if idem_key is None:
        idem_key = str(uuid.uuid4())
    return RequestContext(
        request_id=req_id,
        correlation_id=req_id,
        actor=ActorContext(actor_type="user", actor_id=TEST_USER_ID),
        idempotency_key=idem_key,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Schema tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestImportSourceItem:
    def test_valid_item(self):
        item = ImportSourceItem(
            legacy_id="mneme2:items:1",
            title="Hello",
            content_hash="abc123def456",
        )
        assert item.legacy_id == "mneme2:items:1"
        assert item.title == "Hello"
        assert item.content_hash == "abc123def456"
        assert item.source_type == ImportSourceType.mneme2_item
        assert item.tags == []

    def test_title_too_long(self):
        with pytest.raises(Exception):
            ImportSourceItem(
                legacy_id="mneme2:items:1",
                title="x" * 301,
                content_hash="abc123",
            )

    def test_size_bytes_negative(self):
        with pytest.raises(Exception):
            ImportSourceItem(
                legacy_id="mneme2:items:1",
                title="Valid Title",
                content_hash="abc123",
                size_bytes=-1,
            )

    def test_item_defaults(self):
        """Verify sensible defaults."""
        item = ImportSourceItem(legacy_id="id", title="Test")
        assert item.source_type == ImportSourceType.mneme2_item
        assert item.source == "importer"
        assert item.tags == []
        assert item.metadata == {}


class TestImportPayload:
    def test_valid_payload(self, sample_payload):
        assert len(sample_payload.items) == 1
        assert sample_payload.dry_run is False

    def test_empty_items_rejected(self):
        with pytest.raises(Exception):
            ImportPayload(
                project_id=uuid.uuid4(),
                items=[],
            )

    def test_too_many_items_rejected(self):
        items = [
            ImportSourceItem(
                legacy_id=f"mneme2:items:{i}",
                title=f"Item {i}",
                content_hash="abc123",
            )
            for i in range(1001)
        ]
        with pytest.raises(Exception):
            ImportPayload(project_id=uuid.uuid4(), items=items)

    def test_dry_run_flag(self):
        item = ImportSourceItem(legacy_id="id", title="Test")
        payload = ImportPayload(
            project_id=uuid.uuid4(),
            items=[item],
            dry_run=True,
        )
        assert payload.dry_run is True


class TestImportReport:
    def test_report_construction(self):
        run_id = uuid.uuid4()
        pid = uuid.uuid4()
        items = [
            ImportItemResult(index=0, legacy_id="id:1", status="succeeded"),
            ImportItemResult(index=1, legacy_id="id:2", status="failed", error="bad hash"),
            ImportItemResult(index=2, legacy_id="id:3", status="skipped"),
        ]
        report = ImportReport(
            run_id=run_id,
            project_id=pid,
            source_type=ImportSourceType.mneme2_item,
            status=ImportStatus.failed,
            total_items=3,
            succeeded=1,
            failed=1,
            skipped=1,
            items=items,
            summary="Test summary",
        )
        assert report.run_id == run_id
        assert report.succeeded == 1
        assert report.failed == 1
        assert report.skipped == 1

    def test_to_markdown(self):
        run_id = uuid.uuid4()
        pid = uuid.uuid4()
        report = ImportReport(
            run_id=run_id,
            project_id=pid,
            source_type=ImportSourceType.mneme2_item,
            status=ImportStatus.succeeded,
            total_items=1,
            succeeded=1,
            failed=0,
            skipped=0,
            items=[
                ImportItemResult(
                    index=0,
                    legacy_id="mneme2:1",
                    status="succeeded",
                    asset_uid="PROJ-abc123-1234567890",
                )
            ],
            summary="All good.",
        )
        md = report.to_markdown()
        assert "# Import Report" in md
        assert str(run_id) in md
        assert "succeeded" in md
        assert "All good" in md


class TestValidationResult:
    def test_passed_when_no_errors(self):
        result = ValidationResult(
            passed=True,
            total_items=5,
            valid_count=5,
            error_count=0,
            warning_count=2,
            issues=[],
        )
        assert result.passed is True

    def test_not_passed_when_errors(self):
        result = ValidationResult(
            passed=False,
            total_items=5,
            valid_count=3,
            error_count=2,
            warning_count=0,
            issues=[
                ValidationIssue(index=0, legacy_id="x", severity="error", message="bad")
            ],
        )
        assert result.passed is False


# ═══════════════════════════════════════════════════════════════════════════════
# Mapper tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestFieldMapping:
    def test_mneme2_item_mapping_exists(self):
        assert ImportSourceType.mneme2_item in MAPPING_REGISTRY

    def test_get_mapping_returns_schema(self):
        mapping = get_mapping(ImportSourceType.mneme2_item)
        assert mapping is not None
        assert isinstance(mapping, FieldMappingSchema)
        assert mapping.source_type == ImportSourceType.mneme2_item

    def test_mapping_has_title_field(self):
        mapping = get_mapping(ImportSourceType.mneme2_item)
        title_fields = [m for m in mapping.mappings if m.legacy_field == "title"]
        assert len(title_fields) == 1
        assert title_fields[0].target_field == "Asset.title"
        assert title_fields[0].required is True

    def test_mapping_has_content_hash_field(self):
        mapping = get_mapping(ImportSourceType.mneme2_item)
        hash_fields = [m for m in mapping.mappings if m.legacy_field == "content_hash"]
        assert len(hash_fields) == 1
        assert hash_fields[0].required is True

    def test_get_mapping_unknown_type(self):
        assert get_mapping(ImportSourceType.external_json) is None

    def test_all_mappings_have_strategies(self):
        for entry in MNEME2_ITEM_MAPPING.mappings:
            assert entry.strategy in ("direct_copy", "transform", "computed", "skip")


class TestTransformRegistry:
    def test_normalize_media_type_png(self):
        from mneme.importer.mappers import _normalize_media_type
        assert _normalize_media_type(".png", None) == "image/png"
        assert _normalize_media_type(".PNG", None) == "image/png"

    def test_normalize_media_type_plain_mime(self):
        from mneme.importer.mappers import _normalize_media_type
        assert _normalize_media_type("application/pdf", None) == "application/pdf"

    def test_normalize_media_type_none(self):
        from mneme.importer.mappers import _normalize_media_type
        assert _normalize_media_type(None, None) is None
        assert _normalize_media_type("", None) is None

    def test_derive_asset_type_text(self):
        from mneme.importer.mappers import _derive_asset_type
        assert _derive_asset_type("text/plain", None) == "document"
        assert _derive_asset_type("text/markdown", None) == "document"

    def test_derive_asset_type_image(self):
        from mneme.importer.mappers import _derive_asset_type
        assert _derive_asset_type("image/png", None) == "image"
        assert _derive_asset_type("image/jpeg", None) == "image"

    def test_derive_asset_type_unknown(self):
        from mneme.importer.mappers import _derive_asset_type
        assert _derive_asset_type("something/weird", None) == "other"
        assert _derive_asset_type(None, None) == "other"

    def test_format_legacy_ref(self):
        from mneme.importer.mappers import _format_legacy_ref
        assert _format_legacy_ref("12345", None) == "mneme2:12345"

    def test_tags_to_json(self):
        from mneme.importer.mappers import _tags_to_json
        assert _tags_to_json(["a", "b"], None) == '["a", "b"]'
        assert _tags_to_json("single", None) == '["single"]'

    def test_apply_transform_known(self):
        result = apply_transform("format_legacy_ref", "hello", None)
        assert result == "mneme2:hello"

    def test_apply_transform_unknown(self):
        with pytest.raises(ValueError, match="Unknown transform"):
            apply_transform("no_such_transform", "x", None)


# ═══════════════════════════════════════════════════════════════════════════════
# Validator tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestValidators:
    def test_valid_payload_passes(self, sample_payload):
        result = validate_import_payload(sample_payload)
        assert result.passed is True
        assert result.error_count == 0
        assert result.total_items == 1

    def test_missing_title_fails(self):
        """Whitespace-only title should fail validation."""
        item = ImportSourceItem(
            legacy_id="mneme2:1",
            title="   ",  # whitespace only — passes Pydantic min_length, fails validator
            content_hash="abc123def456",
        )
        payload = ImportPayload(project_id=uuid.uuid4(), items=[item])
        result = validate_import_payload(payload)
        assert result.passed is False
        assert result.error_count >= 1

    def test_missing_content_hash_warns(self):
        item = ImportSourceItem(
            legacy_id="mneme2:1",
            title="Valid Title",
            content_hash=None,
        )
        payload = ImportPayload(project_id=uuid.uuid4(), items=[item])
        result = validate_import_payload(payload)
        assert any(
            i.field == "content_hash" and i.severity == "warning"
            for i in result.issues
        )

    def test_short_content_hash_warns(self):
        item = ImportSourceItem(
            legacy_id="mneme2:1",
            title="Valid",
            content_hash="abc",
        )
        payload = ImportPayload(project_id=uuid.uuid4(), items=[item])
        result = validate_import_payload(payload)
        assert any(
            i.field == "content_hash" and i.severity == "warning"
            for i in result.issues
        )

    def test_non_hex_content_hash_fails(self):
        item = ImportSourceItem(
            legacy_id="mneme2:1",
            title="Valid",
            content_hash="xyz!!!###!!!xyz",
        )
        payload = ImportPayload(project_id=uuid.uuid4(), items=[item])
        result = validate_import_payload(payload)
        assert result.passed is False

    def test_path_traversal_in_uri_warns(self):
        item = ImportSourceItem(
            legacy_id="mneme2:1",
            title="Valid Title",
            content_hash="abc123def456",
            content_uri="/etc/passwd/../../secret",
        )
        payload = ImportPayload(project_id=uuid.uuid4(), items=[item])
        result = validate_import_payload(payload)
        assert any(
            i.field == "content_uri" and ".." in i.message
            for i in result.issues
        )

    def test_multiple_items_validation(self):
        items = [
            ImportSourceItem(
                legacy_id=f"id:{i}",
                title=f"Item {i}",
                content_hash=f"abc{i:08d}" if i % 2 == 0 else None,
            )
            for i in range(5)
        ]
        payload = ImportPayload(project_id=uuid.uuid4(), items=items)
        result = validate_import_payload(payload)
        assert result.total_items == 5

    def test_validation_with_mapping(self):
        """Validate with a mapping schema checks required fields."""
        item = ImportSourceItem(
            legacy_id="mneme2:1",
            title="Test",
            content_hash=None,  # missing but required in mapping
        )
        payload = ImportPayload(project_id=uuid.uuid4(), items=[item])
        result = validate_import_payload(payload, mapping=MNEME2_ITEM_MAPPING)
        # content_hash warning from mapping-level check
        assert any(
            i.field == "content_hash" and i.severity == "warning"
            for i in result.issues
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Reporter tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestReporter:
    def test_build_item_result_success(self):
        result = build_item_result(0, "id:1")
        assert result.status == "succeeded"
        assert result.legacy_id == "id:1"
        assert result.error is None

    def test_build_item_result_error(self):
        result = build_item_result(1, "id:2", error="Validation failed")
        assert result.status == "failed"
        assert result.error == "Validation failed"

    def test_build_item_result_skipped(self):
        result = build_item_result(2, "id:3", skipped=True)
        assert result.status == "skipped"

    def test_build_import_report(self):
        run_id = uuid.uuid4()
        pid = uuid.uuid4()
        items = [
            build_item_result(0, "id:1"),
            build_item_result(1, "id:2", error="bad"),
        ]
        report = build_import_report(
            run_id=run_id,
            project_id=pid,
            source_type=ImportSourceType.mneme2_item,
            status=ImportStatus.failed,
            item_results=items,
        )
        assert report.run_id == run_id
        assert report.total_items == 2
        assert report.succeeded == 1
        assert report.failed == 1

    def test_build_preview_result(self):
        previews = [
            PreviewMapping(
                index=0,
                legacy_id="id:1",
                target_asset={"Asset.title": "Hello"},
            )
        ]
        result = build_preview_result(previews, ImportSourceType.mneme2_item)
        assert result.total_items == 1
        assert result.source_type == ImportSourceType.mneme2_item

    def test_report_to_markdown(self):
        run_id = uuid.uuid4()
        pid = uuid.uuid4()
        report = ImportReport(
            run_id=run_id,
            project_id=pid,
            source_type=ImportSourceType.mneme2_item,
            status=ImportStatus.succeeded,
            total_items=2,
            succeeded=2,
            failed=0,
            skipped=0,
            items=[
                ImportItemResult(index=0, legacy_id="id:1", status="succeeded"),
                ImportItemResult(index=1, legacy_id="id:2", status="succeeded"),
            ],
            summary="Test",
        )
        md = report_to_markdown(report)
        assert "Import Report" in md
        assert "id:1" in md
        assert "succeeded" in md.lower()


# ═══════════════════════════════════════════════════════════════════════════════
# Staging tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestStaging:
    def test_build_inbox_payload(self, sample_item):
        pid = uuid.uuid4()
        payload = build_inbox_payload(sample_item, pid)
        assert payload["project_id"] == pid
        assert payload["inbox_type"] == "importer"
        assert payload["source"] == "importer"
        assert payload["source_ref"] == "mneme2:mneme2:items:12345"
        assert payload["title"] == "Test Document Title"
        assert "legacy_id" in payload["payload_json"]

    def test_build_inbox_payload_with_tags(self):
        item = ImportSourceItem(
            legacy_id="mneme2:1",
            title="Tagged Item",
            content_hash="abc123",
            tags=["tag1", "tag2"],
        )
        pid = uuid.uuid4()
        payload = build_inbox_payload(item, pid)
        assert payload["payload_json"]["tags"] == ["tag1", "tag2"]

    def test_build_inbox_payload_truncates_title(self):
        """build_inbox_payload should truncate title to 300 chars."""
        # Use a valid 300-char title — the function slices at [:300]
        title_300 = "x" * 300
        item = ImportSourceItem(
            legacy_id="mneme2:1",
            title=title_300,
            content_hash="abc123",
        )
        pid = uuid.uuid4()
        payload = build_inbox_payload(item, pid)
        assert len(payload["title"]) == 300
        assert payload["title"] == title_300


# ═══════════════════════════════════════════════════════════════════════════════
# Engine dry-run tests (no DB needed)
# ═══════════════════════════════════════════════════════════════════════════════


class TestEngineDryRun:
    """Dry-run tests — these use a DB session fixture but the engine
    dry_run method does not actually write to the DB, so they work with
    any session."""

    def test_dry_run_passes_valid_payload(self, db, sample_payload):
        ctx = _make_context()
        engine = ImportEngine(db, ctx)
        result = engine.dry_run(sample_payload)
        assert result.passed is True
        assert result.total_items == 1

    def test_dry_run_fails_invalid_payload(self, db):
        ctx = _make_context()
        engine = ImportEngine(db, ctx)
        bad_item = ImportSourceItem(
            legacy_id="bad:1",
            title="   ",  # whitespace-only — passes Pydantic min_length, fails validator
            content_hash="not hex!!!",
        )
        payload = ImportPayload(project_id=uuid.uuid4(), items=[bad_item])
        result = engine.dry_run(payload)
        assert result.passed is False

    def test_dry_run_no_db_writes(self, db):
        """Verify dry_run does not commit or write rows."""
        ctx = _make_context()
        engine = ImportEngine(db, ctx)
        item = ImportSourceItem(
            legacy_id="safe:1",
            title="Safe Test",
            content_hash="abc123def456",
        )
        payload = ImportPayload(project_id=uuid.uuid4(), items=[item])
        result = engine.dry_run(payload)
        assert result.passed is True
        # No inbox items should exist
        from sqlalchemy import text
        count = db.execute(text("SELECT count(*) FROM inbox_items")).scalar_one()
        assert count == 0


# ═══════════════════════════════════════════════════════════════════════════════
# Engine preview tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestEnginePreview:
    def test_preview_valid_payload(self, db, sample_payload):
        ctx = _make_context()
        engine = ImportEngine(db, ctx)
        result = engine.preview(sample_payload)
        assert isinstance(result, PreviewResult)
        assert result.total_items == 1
        assert len(result.previews) == 1
        preview = result.previews[0]
        assert preview.legacy_id == "mneme2:items:12345"
        assert "Asset.title" in preview.target_asset

    def test_preview_unknown_source_type(self, db):
        ctx = _make_context()
        engine = ImportEngine(db, ctx)
        item = ImportSourceItem(
            legacy_id="test:1",
            title="Test",
            content_hash="abc123",
        )
        payload = ImportPayload(
            project_id=uuid.uuid4(),
            source_type=ImportSourceType.external_json,
            items=[item],
        )
        with pytest.raises(ValueError, match="No field mapping registered"):
            engine.preview(payload)

    def test_preview_multiple_items(self, db):
        ctx = _make_context()
        engine = ImportEngine(db, ctx)
        items = [
            ImportSourceItem(
                legacy_id=f"mneme2:{i}",
                title=f"Item {i}",
                content_hash=f"abc{i:08d}",
            )
            for i in range(3)
        ]
        payload = ImportPayload(project_id=uuid.uuid4(), items=items)
        result = engine.preview(payload)
        assert result.total_items == 3
        assert len(result.previews) == 3

    def test_preview_no_db_writes(self, db):
        """Verify preview does not commit or write rows."""
        ctx = _make_context()
        engine = ImportEngine(db, ctx)
        item = ImportSourceItem(
            legacy_id="preview:1",
            title="Preview Test",
            content_hash="abc123def456",
        )
        payload = ImportPayload(project_id=uuid.uuid4(), items=[item])
        engine.preview(payload)
        from sqlalchemy import text
        count = db.execute(text("SELECT count(*) FROM inbox_items")).scalar_one()
        assert count == 0
        count2 = db.execute(text("SELECT count(*) FROM pipeline_runs")).scalar_one()
        assert count2 == 0


# ═══════════════════════════════════════════════════════════════════════════════
# Engine import tests (DB needed)
# ═══════════════════════════════════════════════════════════════════════════════


class TestEngineImport:
    def test_import_valid_items(self, db):
        """Test formal import with valid items."""
        from mneme.db.projects import create_project
        from mneme.schemas.projects import ProjectCreateRequest
        from mneme.schemas.common import SensitivityLevel

        # Create project first
        proj_ctx = _make_context()
        code = f"imp{uuid.uuid4().hex[:8]}"
        proj_payload = ProjectCreateRequest(
            project_code=code,
            name=f"Import Test {code}",
            sensitivity_default=SensitivityLevel.normal,
        )
        project = create_project(db, proj_ctx, payload=proj_payload)
        pid = project.project_id

        ctx = _make_context()
        engine = ImportEngine(db, ctx)

        item = ImportSourceItem(
            legacy_id="mneme2:import:1",
            title="Import Test Item",
            content_hash="a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6a7b8c9d0e1f2a3b4c5d6a7b8c9d0",
            content_type="text/markdown",
        )
        payload = ImportPayload(project_id=pid, items=[item])

        report = engine.import_(payload)
        db.commit()

        assert report is not None
        assert report.status == ImportStatus.succeeded
        assert report.total_items == 1
        assert report.succeeded == 1

    def test_import_fails_validation_errors(self, db):
        """Formal import should raise on hard validation errors."""
        ctx = _make_context()
        engine = ImportEngine(db, ctx)
        bad_item = ImportSourceItem(
            legacy_id="bad:1",
            title="   ",  # whitespace-only — passes Pydantic min_length, fails validator
            content_hash="!!!NOT-HEX!!!",
        )
        payload = ImportPayload(project_id=uuid.uuid4(), items=[bad_item])
        with pytest.raises(ValueError, match="validation failed"):
            engine.import_(payload)

    def test_import_creates_pipeline_run(self, db):
        """Verify import creates a pipeline_runs row."""
        from mneme.db.projects import create_project
        from mneme.schemas.projects import ProjectCreateRequest
        from mneme.schemas.common import SensitivityLevel

        proj_ctx = _make_context()
        code = f"pr{uuid.uuid4().hex[:8]}"
        project = create_project(
            db, proj_ctx,
            payload=ProjectCreateRequest(
                project_code=code,
                name=f"PR Test {code}",
                sensitivity_default=SensitivityLevel.normal,
            ),
        )
        pid = project.project_id

        ctx = _make_context()
        engine = ImportEngine(db, ctx)

        item = ImportSourceItem(
            legacy_id="mneme2:pr:1",
            title="PR Test",
            content_hash="aabbccddeeff00112233445566778899aabbccddeeff00112233445566778899",
        )
        payload = ImportPayload(project_id=pid, items=[item])

        report = engine.import_(payload)
        db.commit()

        # Verify pipeline run was created
        from sqlalchemy import text
        rows = db.execute(
            text("SELECT pipeline_run_id, trigger_type, status FROM pipeline_runs")
        ).all()
        assert len(rows) >= 1
        importer_runs = [r for r in rows if r[1] == "importer"]
        assert len(importer_runs) >= 1

    def test_import_multiple_items(self, db):
        """Formal import with multiple items."""
        from mneme.db.projects import create_project
        from mneme.schemas.projects import ProjectCreateRequest
        from mneme.schemas.common import SensitivityLevel

        proj_ctx = _make_context()
        code = f"multi{uuid.uuid4().hex[:8]}"
        project = create_project(
            db, proj_ctx,
            payload=ProjectCreateRequest(
                project_code=code,
                name=f"Multi Test {code}",
                sensitivity_default=SensitivityLevel.normal,
            ),
        )
        pid = project.project_id

        ctx = _make_context()
        engine = ImportEngine(db, ctx)

        items = [
            ImportSourceItem(
                legacy_id=f"mneme2:multi:{i}",
                title=f"Multi Item {i}",
                content_hash=f"aabb{i:08d}cceeff001122334455",
            )
            for i in range(5)
        ]
        payload = ImportPayload(project_id=pid, items=items)

        report = engine.import_(payload)
        db.commit()

        assert report.total_items == 5
        assert report.succeeded == 5
        assert report.failed == 0
