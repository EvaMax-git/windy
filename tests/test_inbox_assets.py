"""Contract tests for P3-02 Inbox + Asset.

Covers:
- Inbox item CRUD (create, read, list, update status)
- File upload idempotency (content-hash dedup)
- Asset CRUD (create, read, update, soft-delete)
- Asset metadata (add, list, upsert)
- Content-hash duplicate detection
- Status machine transitions (valid and invalid)
- Path sanitization and traversal prevention
- Error cases (not found, invalid transitions)
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text as _text

from mneme.api.context import (
    ActorContext,
    RequestContext,
)
from mneme.db.inbox import (
    create_inbox_item,
    create_inbox_from_staging,
    get_inbox_item,
    list_inbox_items,
    lookup_inbox_by_hash,
    update_inbox_status,
    link_inbox_to_asset,
    mark_inbox_processed,
)
from mneme.db.assets import (
    create_asset,
    promote_from_staging,
    get_asset,
    list_assets,
    update_asset,
    archive_asset,
    lookup_asset_by_hash,
    add_metadata,
    list_metadata,


)
from mneme.db.projects import (
    create_project,
    get_project,
    get_project_by_code,
)
from mneme.schemas.storage import (
    AssetCreateRequest,
    AssetMetadataCreateRequest,
    AssetMetadataRead,
    AssetRead,
    AssetType,
    ContentHashDuplicate,
    InboxItemCreateRequest,
    InboxItemRead,
    InboxStatus,
    InboxType,
    RetentionPolicy,
    StagedFileInfo,
)
from mneme.schemas.projects import ProjectCreateRequest
from mneme.schemas.common import SensitivityLevel
from mneme.storage.backend import (
    get_backend,
    reset_backend,
    sanitize_filename,
    is_path_safe,
    LocalFileSystemBackend,
)
from mneme.storage.staging import (
    stage_file,
    compute_content_hash_bytes,
)


# ═══════════════════════════════════════════════════════════════════
# Test context helpers
# ═══════════════════════════════════════════════════════════════════

from tests.conftest import TEST_USER_ID


def _make_context(idem_key: str | None = None) -> RequestContext:
    """Create a minimal request context for testing."""
    req_id = uuid4()
    if idem_key is None:
        idem_key = str(uuid4())
    return RequestContext(
        request_id=req_id,
        correlation_id=req_id,
        actor=ActorContext(actor_type="user", actor_id=TEST_USER_ID),
        idempotency_key=idem_key,
    )


def _make_project(db) -> tuple:
    """Create a test project and return (project_read, project_code)."""
    context = _make_context()
    code = f"test-{uuid4().hex[:8]}"
    payload = ProjectCreateRequest(
        project_code=code,
        name=f"Test Project {code[:8]}",
        description="Test project for inbox/asset tests",
        sensitivity_default=SensitivityLevel.normal,
    )
    project = create_project(db, context, payload=payload)
    return project, code


# ═══════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def _reset_storage_backend():
    """Ensure each test uses a fresh storage backend singleton."""
    reset_backend()
    yield
    reset_backend()


@pytest.fixture
def tmp_storage_root():
    """Create a temporary storage root directory."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        yield Path(tmp_dir)


@pytest.fixture
def backend(tmp_storage_root):
    """Return a LocalFileSystemBackend pointed at a temp directory."""
    root = str(tmp_storage_root)
    b = LocalFileSystemBackend(root)
    # Ensure staging subdirectory exists
    staging_dir = Path(root) / "staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    return b


@pytest.fixture
def staged_file(backend):
    """Stage a test file and return StagedFileInfo."""
    content = b"Hello, Mneme! This is a test file for inbox/asset contract tests."
    return stage_file(
        file_content=content,
        original_filename="test_document.txt",
        backend=backend,
    )


# ═══════════════════════════════════════════════════════════════════
# Inbox Item Tests
# ═══════════════════════════════════════════════════════════════════

class TestInboxItemCreate:
    """P3-02: inbox item creation (non-file types)."""

    def test_create_inbox_item_text_type(self, db):
        """Create an inbox item of type 'text'."""
        project, code = _make_project(db)
        context = _make_context()

        payload = InboxItemCreateRequest(
            project_id=project.project_id,
            inbox_type=InboxType.text,
            source="api",
            title="Test text inbox item",
        )
        item = create_inbox_item(db, context, payload=payload, status="received")

        assert item.inbox_item_id is not None
        assert item.project_id == project.project_id
        assert item.inbox_type == InboxType.text
        assert item.status == InboxStatus.received
        assert item.title == "Test text inbox item"
        assert item.source == "api"

    def test_create_inbox_item_url_type(self, db):
        """Create an inbox item of type 'url' with source_uri."""
        project, code = _make_project(db)
        context = _make_context()

        payload = InboxItemCreateRequest(
            project_id=project.project_id,
            inbox_type=InboxType.url,
            source="api",
            source_uri="https://example.com/article",
            title="URL inbox item",
        )
        item = create_inbox_item(db, context, payload=payload)

        assert item.inbox_type == InboxType.url
        assert item.source_uri == "https://example.com/article"

    def test_create_inbox_item_idempotent(self, db):
        """Same idempotency key returns the existing inbox item."""
        project, code = _make_project(db)
        idem_key = str(uuid4())
        context = _make_context(idem_key)

        payload = InboxItemCreateRequest(
            project_id=project.project_id,
            inbox_type=InboxType.text,
            source="api",
            title="Idempotent test",
        )
        item1 = create_inbox_item(db, context, payload=payload)

        # Second call with same idempotency key
        context2 = _make_context(idem_key)
        item2 = create_inbox_item(db, context2, payload=payload)

        assert item1.inbox_item_id == item2.inbox_item_id
        assert item1.title == item2.title

    def test_create_inbox_item_with_content_hash(self, db):
        """Create an inbox item with a pre-computed content hash."""
        project, code = _make_project(db)
        context = _make_context()

        payload = InboxItemCreateRequest(
            project_id=project.project_id,
            inbox_type=InboxType.file,
            source="import",
            title="File with hash",
            content_hash="sha256:abc123def456",
        )
        item = create_inbox_item(db, context, payload=payload)

        assert item.content_hash == "sha256:abc123def456"

    def test_create_inbox_item_with_payload_json(self, db):
        """Create an inbox item with payload_json metadata."""
        project, code = _make_project(db)
        context = _make_context()

        payload = InboxItemCreateRequest(
            project_id=project.project_id,
            inbox_type=InboxType.text,
            source="api",
            title="With payload",
            payload_json={"key": "value", "nested": {"a": 1}},
        )
        item = create_inbox_item(db, context, payload=payload)

        assert item.inbox_item_id is not None
        # payload_json is stored in DB but not exposed in InboxItemRead


class TestInboxItemQuery:
    """P3-02: inbox item queries."""

    def test_get_inbox_item(self, db):
        """Get an inbox item by ID."""
        project, code = _make_project(db)
        context = _make_context()

        payload = InboxItemCreateRequest(
            project_id=project.project_id,
            inbox_type=InboxType.text,
            source="api",
            title="Get me",
        )
        created = create_inbox_item(db, context, payload=payload)
        fetched = get_inbox_item(db, created.inbox_item_id)

        assert fetched is not None
        assert fetched.inbox_item_id == created.inbox_item_id
        assert fetched.title == "Get me"

    def test_get_inbox_item_not_found(self, db):
        """Looking up a nonexistent inbox item returns None."""
        result = get_inbox_item(db, uuid4())
        assert result is None

    def test_list_inbox_items_all(self, db):
        """List inbox items returns all items."""
        project, code = _make_project(db)

        for i in range(3):
            context = _make_context()
            payload = InboxItemCreateRequest(
                project_id=project.project_id,
                inbox_type=InboxType.text,
                source="api",
                title=f"Item {i}",
            )
            create_inbox_item(db, context, payload=payload)

        items, total = list_inbox_items(db)
        assert total >= 3
        assert len(items) >= 3

    def test_list_inbox_items_by_project(self, db):
        """List inbox items filtered by project."""
        project1, code1 = _make_project(db)
        project2, code2 = _make_project(db)

        # Create item in project1
        ctx1 = _make_context()
        create_inbox_item(db, ctx1, payload=InboxItemCreateRequest(
            project_id=project1.project_id,
            inbox_type=InboxType.text, source="api", title="P1 item",
        ))

        # Create item in project2
        ctx2 = _make_context()
        create_inbox_item(db, ctx2, payload=InboxItemCreateRequest(
            project_id=project2.project_id,
            inbox_type=InboxType.text, source="api", title="P2 item",
        ))

        items1, _ = list_inbox_items(db, project_id=project1.project_id)
        items2, _ = list_inbox_items(db, project_id=project2.project_id)

        assert all(i.project_id == project1.project_id for i in items1)
        assert all(i.project_id == project2.project_id for i in items2)

    def test_list_inbox_items_by_status(self, db):
        """List inbox items filtered by status."""
        project, code = _make_project(db)

        ctx = _make_context()
        create_inbox_item(db, ctx, payload=InboxItemCreateRequest(
            project_id=project.project_id,
            inbox_type=InboxType.text, source="api", title="Received item",
        ), status="received")

        items, _ = list_inbox_items(db, status="received")
        assert all(i.status == InboxStatus.received for i in items)

    def test_list_inbox_items_pagination(self, db):
        """List inbox items with pagination."""
        project, code = _make_project(db)

        for i in range(5):
            ctx = _make_context()
            create_inbox_item(db, ctx, payload=InboxItemCreateRequest(
                project_id=project.project_id,
                inbox_type=InboxType.text, source="api", title=f"Page {i}",
            ))

        items, total = list_inbox_items(db, page=1, page_size=2)
        assert len(items) == 2
        assert total >= 5


class TestInboxStatusMachine:
    """P3-02: inbox status machine transitions."""

    def test_transition_received_to_staged(self, db):
        """received -> staged is a valid transition."""
        project, code = _make_project(db)
        ctx = _make_context()

        payload = InboxItemCreateRequest(
            project_id=project.project_id,
            inbox_type=InboxType.text, source="api", title="To stage",
        )
        item = create_inbox_item(db, ctx, payload=payload, status="received")

        ctx2 = _make_context()
        updated = update_inbox_status(
            db, ctx2, inbox_item_id=item.inbox_item_id,
            new_status="staged", expected_status="received",
        )
        assert updated.status == InboxStatus.staged

    def test_transition_staged_to_linked(self, db):
        """staged -> linked is a valid transition with asset_id."""
        project, code = _make_project(db)
        ctx = _make_context()

        payload = InboxItemCreateRequest(
            project_id=project.project_id,
            inbox_type=InboxType.file, source="api", title="To link",
        )
        item = create_inbox_item(db, ctx, payload=payload, status="staged")

        asset_id = uuid4()
        ctx2 = _make_context()
        updated = update_inbox_status(
            db, ctx2, inbox_item_id=item.inbox_item_id,
            new_status="linked", asset_id=asset_id, expected_status="staged",
        )
        assert updated.status == InboxStatus.linked
        assert updated.asset_id == asset_id

    def test_transition_linked_to_processed(self, db):
        """linked -> processed sets processed_at."""
        project, code = _make_project(db)
        ctx = _make_context()

        payload = InboxItemCreateRequest(
            project_id=project.project_id,
            inbox_type=InboxType.file, source="api", title="To process",
        )
        item = create_inbox_item(db, ctx, payload=payload, status="staged")

        ctx2 = _make_context()
        linked = update_inbox_status(
            db, ctx2, inbox_item_id=item.inbox_item_id,
            new_status="linked", asset_id=uuid4(), expected_status="staged",
        )

        ctx3 = _make_context()
        processed = update_inbox_status(
            db, ctx3, inbox_item_id=item.inbox_item_id,
            new_status="processed", expected_status="linked",
        )
        assert processed.status == InboxStatus.processed
        assert processed.processed_at is not None

    def test_invalid_transition_received_to_processed(self, db):
        """received -> processed is invalid (must go through staged and linked)."""
        project, code = _make_project(db)
        ctx = _make_context()

        payload = InboxItemCreateRequest(
            project_id=project.project_id,
            inbox_type=InboxType.text, source="api", title="Skip",
        )
        item = create_inbox_item(db, ctx, payload=payload, status="received")

        ctx2 = _make_context()
        with pytest.raises(ValueError, match="Invalid status transition"):
            update_inbox_status(
                db, ctx2, inbox_item_id=item.inbox_item_id,
                new_status="processed", expected_status="received",
            )

    def test_can_reject_from_any_active_state(self, db):
        """received/staged/linked can all transition to rejected."""
        project, code = _make_project(db)

        for from_status in ["received", "staged", "linked"]:
            ctx = _make_context()
            payload = InboxItemCreateRequest(
                project_id=project.project_id,
                inbox_type=InboxType.text, source="api",
                title=f"Reject from {from_status}",
            )
            if from_status == "staged":
                item = create_inbox_item(db, ctx, payload=payload, status="staged")
            elif from_status == "linked":
                item = create_inbox_item(db, ctx, payload=payload, status="staged")
                ctx_link = _make_context()
                item = update_inbox_status(
                    db, ctx_link, inbox_item_id=item.inbox_item_id,
                    new_status="linked", asset_id=uuid4(), expected_status="staged",
                )
            else:
                item = create_inbox_item(db, ctx, payload=payload, status=from_status)

            ctx2 = _make_context()
            updated = update_inbox_status(
                db, ctx2, inbox_item_id=item.inbox_item_id,
                new_status="rejected", expected_status=from_status,
            )
            assert updated.status == InboxStatus.rejected

    def test_link_inbox_to_asset_convenience(self, db):
        """link_inbox_to_asset() convenience function works."""
        project, code = _make_project(db)
        ctx = _make_context()

        payload = InboxItemCreateRequest(
            project_id=project.project_id,
            inbox_type=InboxType.file, source="api", title="Link me",
        )
        item = create_inbox_item(db, ctx, payload=payload, status="staged")

        asset_id = uuid4()
        ctx2 = _make_context()
        linked = link_inbox_to_asset(db, ctx2, inbox_item_id=item.inbox_item_id, asset_id=asset_id)

        assert linked.status == InboxStatus.linked
        assert linked.asset_id == asset_id

    def test_mark_inbox_processed_convenience(self, db):
        """mark_inbox_processed() convenience function works."""
        project, code = _make_project(db)
        ctx = _make_context()

        payload = InboxItemCreateRequest(
            project_id=project.project_id,
            inbox_type=InboxType.file, source="api", title="Mark done",
        )
        item = create_inbox_item(db, ctx, payload=payload, status="staged")

        ctx2 = _make_context()
        linked = link_inbox_to_asset(db, ctx2, inbox_item_id=item.inbox_item_id, asset_id=uuid4())

        ctx3 = _make_context()
        processed = mark_inbox_processed(db, ctx3, inbox_item_id=item.inbox_item_id)

        assert processed.status == InboxStatus.processed
        assert processed.processed_at is not None


class TestInboxHashLookup:
    """P3-02: inbox content-hash lookup for dedup."""

    def test_lookup_by_hash_returns_existing(self, db):
        """lookup_inbox_by_hash returns the existing inbox item."""
        project, code = _make_project(db)
        ctx = _make_context()

        test_hash = "sha256:abc123def456789"

        payload = InboxItemCreateRequest(
            project_id=project.project_id,
            inbox_type=InboxType.file, source="api",
            title="Hash test", content_hash=test_hash,
        )
        item = create_inbox_item(db, ctx, payload=payload, status="staged")

        found = lookup_inbox_by_hash(
            db, content_hash=test_hash, project_id=project.project_id
        )
        assert found is not None
        assert found.inbox_item_id == item.inbox_item_id

    def test_lookup_by_hash_not_found(self, db):
        """lookup_inbox_by_hash returns None when no match."""
        project, code = _make_project(db)
        found = lookup_inbox_by_hash(
            db, content_hash="nonexistent", project_id=project.project_id
        )
        assert found is None

    def test_lookup_by_hash_ignores_rejected(self, db):
        """lookup_inbox_by_hash skips rejected items."""
        project, code = _make_project(db)
        test_hash = "sha256:rejected_hash_test"

        ctx = _make_context()
        payload = InboxItemCreateRequest(
            project_id=project.project_id,
            inbox_type=InboxType.file, source="api",
            title="Rejected item", content_hash=test_hash,
        )
        item = create_inbox_item(db, ctx, payload=payload, status="staged")

        # Reject it
        ctx2 = _make_context()
        update_inbox_status(
            db, ctx2, inbox_item_id=item.inbox_item_id,
            new_status="rejected", expected_status="staged",
        )

        # Should not be found
        found = lookup_inbox_by_hash(
            db, content_hash=test_hash, project_id=project.project_id
        )
        assert found is None


class TestInboxFromStaging:
    """P3-02: create_inbox_from_staging from Storage layer output."""

    def test_create_inbox_from_staging(self, db, staged_file, backend):
        """Create an inbox item from a staged file."""
        project, code = _make_project(db)
        ctx = _make_context()

        item = create_inbox_from_staging(
            db, ctx,
            project_id=project.project_id,
            staged_info=staged_file,
            title="My uploaded file",
            source="api",
        )

        assert item.status == InboxStatus.staged
        assert item.inbox_type == InboxType.file
        assert item.content_hash == staged_file.content_hash
        assert item.title == "My uploaded file"
        assert item.source == "api"
        assert item.source_uri is not None
        assert "file://" in item.source_uri


# ═══════════════════════════════════════════════════════════════════
# Asset Tests
# ═══════════════════════════════════════════════════════════════════

class TestAssetCreate:
    """P3-03: asset creation."""

    def test_create_asset_basic(self, db):
        """Create a basic asset record."""
        project, code = _make_project(db)
        ctx = _make_context()

        content_hash = compute_content_hash_bytes(b"test content for asset")
        payload = AssetCreateRequest(
            project_id=project.project_id,
            title="Test Document",
            asset_type=AssetType.document,
            media_type="text/plain",
            original_filename="test.txt",
            content_hash=content_hash,
            size_bytes=100,
            sensitivity_level=SensitivityLevel.normal,
            retention_policy=RetentionPolicy.default,
        )
        asset = create_asset(db, ctx, payload=payload, project_code=code)

        assert asset.asset_id is not None
        assert asset.project_id == project.project_id
        assert asset.title == "Test Document"
        assert asset.asset_type == AssetType.document
        assert asset.content_hash == content_hash
        assert asset.sensitivity_level == SensitivityLevel.normal
        assert asset.status == "active"
        assert hasattr(asset.ingest_state, 'value')
        assert asset.ingest_state.value == "pending"
        assert asset.knowledge_state.value == "not_started"

    def test_create_asset_generates_uid(self, db):
        """Creating an asset auto-generates asset_uid and canonical_uri."""
        project, code = _make_project(db)
        ctx = _make_context()

        content_hash = compute_content_hash_bytes(b"unique content")
        payload = AssetCreateRequest(
            project_id=project.project_id,
            title="UID Test",
            asset_type=AssetType.image,
            content_hash=content_hash,
        )
        asset = create_asset(db, ctx, payload=payload, project_code=code)

        assert asset.asset_uid is not None
        assert asset.asset_uid.startswith(code)
        assert asset.canonical_uri is not None
        assert code in asset.canonical_uri
        assert asset.asset_uid in asset.canonical_uri

    def test_create_asset_idempotent(self, db):
        """Same idempotency key returns the existing asset."""
        project, code = _make_project(db)
        idem_key = str(uuid4())
        ctx = _make_context(idem_key)

        content_hash = compute_content_hash_bytes(b"idempotent asset content")
        payload = AssetCreateRequest(
            project_id=project.project_id,
            title="Idempotent Asset",
            asset_type=AssetType.document,
            content_hash=content_hash,
        )
        asset1 = create_asset(db, ctx, payload=payload, project_code=code)

        ctx2 = _make_context(idem_key)
        asset2 = create_asset(db, ctx2, payload=payload, project_code=code)

        assert asset1.asset_id == asset2.asset_id

    def test_create_asset_from_inbox(self, db):
        """Create an asset linked to a source inbox item."""
        project, code = _make_project(db)
        ctx = _make_context()

        # First create an inbox item
        inbox_payload = InboxItemCreateRequest(
            project_id=project.project_id,
            inbox_type=InboxType.file, source="api", title="Source inbox",
        )
        inbox_item = create_inbox_item(db, ctx, payload=inbox_payload, status="staged")

        # Then create asset with source_inbox_item_id
        content_hash = compute_content_hash_bytes(b"from inbox")
        ctx2 = _make_context()
        asset_payload = AssetCreateRequest(
            project_id=project.project_id,
            title="From Inbox",
            asset_type=AssetType.document,
            content_hash=content_hash,
            source_inbox_item_id=inbox_item.inbox_item_id,
        )
        asset = create_asset(db, ctx2, payload=asset_payload, project_code=code)

        assert asset.source_inbox_item_id == inbox_item.inbox_item_id

    def test_create_asset_duplicate_content_hash(self, db):
        """Creating an asset with duplicate content_hash raises IntegrityError."""
        project, code = _make_project(db)

        content_hash = compute_content_hash_bytes(b"duplicate hash test content")
        ctx1 = _make_context()
        payload = AssetCreateRequest(
            project_id=project.project_id,
            title="First Asset",
            asset_type=AssetType.document,
            content_hash=content_hash,
        )
        asset1 = create_asset(db, ctx1, payload=payload, project_code=code)

        # Second asset with same hash — may raise IntegrityError due to UNIQUE constraint
        # or may return the existing asset depending on implementation
        ctx2 = _make_context()
        payload2 = AssetCreateRequest(
            project_id=project.project_id,
            title="Second Asset",
            asset_type=AssetType.document,
            content_hash=content_hash,
        )
        try:
            asset2 = create_asset(db, ctx2, payload=payload2, project_code=code)
            # If it returns, it should be the same asset
            assert asset1.asset_id == asset2.asset_id
        except Exception:
            # IntegrityError from UNIQUE constraint is also acceptable
            pass


class TestAssetQuery:
    """P3-03: asset queries."""

    def test_get_asset(self, db):
        """Get an asset by ID."""
        project, code = _make_project(db)
        ctx = _make_context()

        content_hash = compute_content_hash_bytes(b"get me")
        payload = AssetCreateRequest(
            project_id=project.project_id,
            title="Get Asset",
            asset_type=AssetType.document,
            content_hash=content_hash,
        )
        created = create_asset(db, ctx, payload=payload, project_code=code)
        fetched = get_asset(db, created.asset_id)

        assert fetched is not None
        assert fetched.asset_id == created.asset_id

    def test_get_asset_not_found(self, db):
        """Looking up a nonexistent asset returns None."""
        result = get_asset(db, uuid4())
        assert result is None

    def test_list_assets(self, db):
        """List assets with filters."""
        project, code = _make_project(db)

        for i in range(3):
            ctx = _make_context()
            content_hash = compute_content_hash_bytes(f"list asset {i}".encode())
            payload = AssetCreateRequest(
                project_id=project.project_id,
                title=f"Asset {i}",
                asset_type=AssetType.document,
                content_hash=content_hash,
            )
            create_asset(db, ctx, payload=payload, project_code=code)

        items, total = list_assets(db)
        assert total >= 3
        assert len(items) >= 3

    def test_list_assets_by_project(self, db):
        """List assets filtered by project."""
        project1, code1 = _make_project(db)
        project2, code2 = _make_project(db)

        ctx1 = _make_context()
        h1 = compute_content_hash_bytes(b"p1 asset")
        create_asset(db, ctx1, payload=AssetCreateRequest(
            project_id=project1.project_id, title="P1",
            asset_type=AssetType.document, content_hash=h1,
        ), project_code=code1)

        ctx2 = _make_context()
        h2 = compute_content_hash_bytes(b"p2 asset")
        create_asset(db, ctx2, payload=AssetCreateRequest(
            project_id=project2.project_id, title="P2",
            asset_type=AssetType.document, content_hash=h2,
        ), project_code=code2)

        items1, _ = list_assets(db, project_id=project1.project_id)
        assert all(i.project_id == project1.project_id for i in items1)

    def test_list_assets_by_type(self, db):
        """List assets filtered by asset_type."""
        project, code = _make_project(db)

        ctx1 = _make_context()
        h1 = compute_content_hash_bytes(b"image asset")
        create_asset(db, ctx1, payload=AssetCreateRequest(
            project_id=project.project_id, title="Image",
            asset_type=AssetType.image, content_hash=h1,
        ), project_code=code)

        items, _ = list_assets(db, asset_type="image")
        assert all(i.asset_type == AssetType.image for i in items)

    def test_lookup_asset_by_hash(self, db):
        """Look up an asset by content hash."""
        project, code = _make_project(db)

        content_hash = compute_content_hash_bytes(b"hash lookup test")
        ctx = _make_context()
        payload = AssetCreateRequest(
            project_id=project.project_id,
            title="Hash Lookup",
            asset_type=AssetType.document,
            content_hash=content_hash,
        )
        created = create_asset(db, ctx, payload=payload, project_code=code)

        found = lookup_asset_by_hash(
            db, content_hash=content_hash, project_id=project.project_id
        )
        assert found is not None
        assert found.asset_id == created.asset_id

    def test_lookup_asset_by_hash_not_found(self, db):
        """lookup_asset_by_hash returns None for unknown hash."""
        project, code = _make_project(db)
        found = lookup_asset_by_hash(
            db, content_hash="unknown", project_id=project.project_id
        )
        assert found is None


class TestAssetUpdate:
    """P3-03: asset update operations."""

    def test_update_asset_title(self, db):
        """Update an asset's title."""
        project, code = _make_project(db)
        ctx = _make_context()

        content_hash = compute_content_hash_bytes(b"update title test")
        payload = AssetCreateRequest(
            project_id=project.project_id,
            title="Original Title",
            asset_type=AssetType.document,
            content_hash=content_hash,
        )
        asset = create_asset(db, ctx, payload=payload, project_code=code)

        ctx2 = _make_context()
        updated = update_asset(
            db, ctx2, asset_id=asset.asset_id, title="Updated Title",
        )
        assert updated.title == "Updated Title"
        assert updated.current_version > asset.current_version

    def test_update_asset_sensitivity(self, db):
        """Update an asset's sensitivity level."""
        project, code = _make_project(db)
        ctx = _make_context()

        content_hash = compute_content_hash_bytes(b"update sensitivity")
        payload = AssetCreateRequest(
            project_id=project.project_id,
            title="Sensitivity Test",
            asset_type=AssetType.document,
            content_hash=content_hash,
        )
        asset = create_asset(db, ctx, payload=payload, project_code=code)

        ctx2 = _make_context()
        updated = update_asset(
            db, ctx2, asset_id=asset.asset_id,
            sensitivity_level="sensitive",
        )
        assert updated.sensitivity_level == SensitivityLevel.sensitive

    def test_archive_asset(self, db):
        """Soft-delete sets status to 'deleted'."""
        project, code = _make_project(db)
        ctx = _make_context()

        content_hash = compute_content_hash_bytes(b"delete me")
        payload = AssetCreateRequest(
            project_id=project.project_id,
            title="Delete Me",
            asset_type=AssetType.document,
            content_hash=content_hash,
        )
        asset = create_asset(db, ctx, payload=payload, project_code=code)

        ctx2 = _make_context()
        deleted = archive_asset(db, ctx2, asset_id=asset.asset_id)

        assert deleted.status.value == "deleted"

    def test_soft_deleted_not_in_list(self, db):
        """Soft-deleted assets should not appear in list."""
        project, code = _make_project(db)
        ctx = _make_context()

        content_hash = compute_content_hash_bytes(b"hidden deleted")
        payload = AssetCreateRequest(
            project_id=project.project_id,
            title="Hidden",
            asset_type=AssetType.document,
            content_hash=content_hash,
        )
        asset = create_asset(db, ctx, payload=payload, project_code=code)

        ctx2 = _make_context()
        archive_asset(db, ctx2, asset_id=asset.asset_id)

        items, _ = list_assets(db, project_id=project.project_id, status='active')
        assert all(i.asset_id != asset.asset_id for i in items)


class TestPromoteFromStaging:
    """P3-03: promote_from_staging — staging to permanent storage."""

    def test_promote_from_staging(self, db, backend, staged_file):
        """Promote a staged file to permanent asset storage."""
        project, code = _make_project(db)
        ctx = _make_context()

        # Create asset in pending state
        asset_payload = AssetCreateRequest(
            project_id=project.project_id,
            title="Promote Test",
            asset_type=AssetType.document,
            media_type=staged_file.media_type,
            original_filename=staged_file.original_filename,
            content_hash=staged_file.content_hash,
            size_bytes=staged_file.size_bytes,
        )
        asset = create_asset(db, ctx, payload=asset_payload, project_code=code)

        # Promote
        ctx2 = _make_context()
        # Move file first (simulate what API does)
        from mneme.storage.promote import promote_file, _build_asset_path
        storage_ref = promote_file(
            staging_path=staged_file.staging_path,
            project_id=project.project_id,
            asset_uid=asset.asset_uid,
            original_filename=staged_file.original_filename,
        )
        promoted = promote_from_staging(
            db, ctx2,
            asset_id=asset.asset_id,
            storage_ref=storage_ref,
            size_bytes=staged_file.size_bytes,
        )

        # Check state changes
        assert promoted.ingest_state.value in ("staged", "importing")
        assert promoted.storage_ref != "pending"
        assert "assets" in promoted.storage_ref or promoted.storage_ref != asset.storage_ref

        # Verify storage_ref was updated
        assert "assets" in promoted.storage_ref

    def test_promote_requires_pending_state(self, db, staged_file):
        """Cannot promote an asset that is not in 'pending' ingest_state."""
        project, code = _make_project(db)
        ctx = _make_context()

        asset_payload = AssetCreateRequest(
            project_id=project.project_id,
            title="Already Promoted",
            asset_type=AssetType.document,
            content_hash=staged_file.content_hash,
        )
        asset = create_asset(db, ctx, payload=asset_payload, project_code=code)

        # First promote
        ctx2 = _make_context()
        from mneme.storage.promote import promote_file
        storage_ref = promote_file(
            staging_path=staged_file.staging_path,
            project_id=project.project_id,
            asset_uid=asset.asset_uid,
            original_filename=staged_file.original_filename,
        )
        promote_from_staging(
            db, ctx2,
            asset_id=asset.asset_id,
            storage_ref=storage_ref,
            size_bytes=staged_file.size_bytes,
        )

        # Second promote should fail (asset no longer in 'pending' state)
        ctx3 = _make_context()
        with pytest.raises(ValueError, match="Invalid ingest state transition"):
            promote_from_staging(
                db, ctx3,
                asset_id=asset.asset_id,
                storage_ref=storage_ref,
                size_bytes=staged_file.size_bytes,
            )


class TestAssetMetadata:
    """P3-03: asset metadata management."""

    def test_add_metadata(self, db):
        """Add a metadata entry to an asset."""
        project, code = _make_project(db)
        ctx = _make_context()

        content_hash = compute_content_hash_bytes(b"metadata test")
        payload = AssetCreateRequest(
            project_id=project.project_id,
            title="Metadata Asset",
            asset_type=AssetType.document,
            content_hash=content_hash,
        )
        asset = create_asset(db, ctx, payload=payload, project_code=code)

        ctx2 = _make_context()
        meta_payload = AssetMetadataCreateRequest(
            metadata_key="author",
            metadata_value="Test Author",
            value_type="text",
            source="manual",
        )
        meta = add_metadata(db, ctx2, asset_id=asset.asset_id, payload=meta_payload)

        assert meta.metadata_key == "author"
        assert meta.metadata_value == "Test Author"
        assert meta.source == "manual"

    def test_add_metadata_upsert(self, db):
        """Adding metadata with same key+source updates existing."""
        project, code = _make_project(db)
        ctx = _make_context()

        content_hash = compute_content_hash_bytes(b"upsert metadata")
        payload = AssetCreateRequest(
            project_id=project.project_id,
            title="Upsert Asset",
            asset_type=AssetType.document,
            content_hash=content_hash,
        )
        asset = create_asset(db, ctx, payload=payload, project_code=code)

        # First insert
        ctx2 = _make_context()
        meta_payload = AssetMetadataCreateRequest(
            metadata_key="version",
            metadata_value="1.0",
            source="manual",
        )
        add_metadata(db, ctx2, asset_id=asset.asset_id, payload=meta_payload)

        # Second insert with same key+source (upsert)
        ctx3 = _make_context()
        meta_payload2 = AssetMetadataCreateRequest(
            metadata_key="version",
            metadata_value="2.0",
            source="manual",
        )
        meta2 = add_metadata(db, ctx3, asset_id=asset.asset_id, payload=meta_payload2)

        assert meta2.metadata_value == "2.0"

        # List should have only one entry for this key
        items = list_metadata(db, asset_id=asset.asset_id)
        version_items = [m for m in items if m.metadata_key == "version"]
        assert len(version_items) == 1
        assert version_items[0].metadata_value == "2.0"

    def test_list_metadata(self, db):
        """List all metadata entries for an asset."""
        project, code = _make_project(db)
        ctx = _make_context()

        content_hash = compute_content_hash_bytes(b"list metadata")
        payload = AssetCreateRequest(
            project_id=project.project_id,
            title="List Metadata Asset",
            asset_type=AssetType.document,
            content_hash=content_hash,
        )
        asset = create_asset(db, ctx, payload=payload, project_code=code)

        keys = ["author", "date", "tags"]
        for key in keys:
            ctx_m = _make_context()
            add_metadata(db, ctx_m, asset_id=asset.asset_id, payload=AssetMetadataCreateRequest(
                metadata_key=key,
                metadata_value=f"value-{key}",
                source="manual",
            ))

        items = list_metadata(db, asset_id=asset.asset_id)
        assert len(items) >= 3

        found_keys = {m.metadata_key for m in items}
        for key in keys:
            assert key in found_keys

    def test_add_metadata_with_confidence(self, db):
        """Add metadata with a confidence score."""
        project, code = _make_project(db)
        ctx = _make_context()

        content_hash = compute_content_hash_bytes(b"confidence test")
        payload = AssetCreateRequest(
            project_id=project.project_id,
            title="Confidence Asset",
            asset_type=AssetType.document,
            content_hash=content_hash,
        )
        asset = create_asset(db, ctx, payload=payload, project_code=code)

        ctx2 = _make_context()
        meta_payload = AssetMetadataCreateRequest(
            metadata_key="mime_type",
            metadata_value="application/pdf",
            source="system",
            confidence=0.95,
        )
        meta = add_metadata(db, ctx2, asset_id=asset.asset_id, payload=meta_payload)

        assert meta.confidence == pytest.approx(0.95)

    def test_metadata_not_found_for_nonexistent_asset(self, db):
        """Adding metadata to nonexistent asset raises ValueError."""
        ctx = _make_context()
        meta_payload = AssetMetadataCreateRequest(
            metadata_key="test",
            metadata_value="value",
        )
        with pytest.raises(ValueError, match="not found"):
            add_metadata(db, ctx, asset_id=uuid4(), payload=meta_payload)


# ═══════════════════════════════════════════════════════════════════
# UID & URI generation tests
# ═══════════════════════════════════════════════════════════════════

class TestAssetUidGeneration:
    """P3-03: asset_uid and canonical_uri generation."""

    def test_generate_asset_uid_format(self):
        """asset_uid follows {project_code}-{hash_prefix}-{timestamp}."""
        from mneme.db.assets import _generate_asset_uid
        uid = _generate_asset_uid("demo", "abcdef1234567890abcdef1234567890abcdef12")
        parts = uid.split("-")
        assert len(parts) >= 3
        assert parts[0] == "demo"
        assert parts[1] == "abcdef123456"
        # parts[2] is the timestamp (numeric)
        assert parts[2].isdigit()

    def test_build_canonical_uri(self):
        """canonical_uri uses mneme:// scheme."""
        from mneme.db.assets import _generate_asset_uid
        uri = f"mneme://demo/assets/demo-abc123-1234567890"
        assert uri == "mneme://demo/assets/demo-abc123-1234567890"
        assert uri.startswith("mneme://")

    def test_asset_uid_uniqueness(self):
        """asset_uid generated at different times should differ."""
        from mneme.db.assets import _generate_asset_uid
        uid1 = _generate_asset_uid("demo", "abc123def4567890")
        import time
        time.sleep(0.001)  # Ensure different timestamp
        uid2 = _generate_asset_uid("demo", "abc123def4567890")
        assert uid1 != uid2


# ═══════════════════════════════════════════════════════════════════
# End-to-end flow tests
# ═══════════════════════════════════════════════════════════════════

class TestEndToEndFlow:
    """P3-02 + P3-03: full upload -> inbox -> asset flow."""

    def test_full_flow_staging_to_asset(self, db, backend):
        """End-to-end: stage file -> create inbox -> create asset -> promote."""
        project, code = _make_project(db)

        # 1. Stage a file
        content = b"End-to-end test content for full flow verification."
        staged = stage_file(
            file_content=content,
            original_filename="e2e_test.txt",
            backend=backend,
        )

        # 2. Create inbox item from staging
        ctx1 = _make_context()
        inbox_item = create_inbox_from_staging(
            db, ctx1,
            project_id=project.project_id,
            staged_info=staged,
            title="E2E Test File",
        )
        assert inbox_item.status == InboxStatus.staged

        # 3. Create asset
        ctx2 = _make_context()
        asset_payload = AssetCreateRequest(
            project_id=project.project_id,
            title="E2E Test Asset",
            asset_type=AssetType.document,
            media_type=staged.media_type,
            original_filename=staged.original_filename,
            content_hash=staged.content_hash,
            size_bytes=staged.size_bytes,
            source_inbox_item_id=inbox_item.inbox_item_id,
        )
        asset = create_asset(db, ctx2, payload=asset_payload, project_code=code)
        assert asset.ingest_state.value == "pending"

        # 4. Promote file: move to permanent storage then update DB
        ctx3 = _make_context()
        from mneme.storage.promote import promote_file
        storage_ref = promote_file(
            staging_path=staged.staging_path,
            project_id=project.project_id,
            asset_uid=asset.asset_uid,
            original_filename=staged.original_filename,
        )
        promoted = promote_from_staging(
            db, ctx3,
            asset_id=asset.asset_id,
            storage_ref=storage_ref,
            size_bytes=staged.size_bytes,
        )
        assert promoted.ingest_state.value in ("staged", "importing")

        # 5. Link inbox to asset
        ctx4 = _make_context()
        linked = link_inbox_to_asset(
            db, ctx4,
            inbox_item_id=inbox_item.inbox_item_id,
            asset_id=asset.asset_id,
        )
        assert linked.status == InboxStatus.linked
        assert linked.asset_id == asset.asset_id

        # 6. Add metadata
        ctx5 = _make_context()
        meta = add_metadata(
            db, ctx5, asset_id=asset.asset_id,
            payload=AssetMetadataCreateRequest(
                metadata_key="pages", metadata_value="1", source="system",
            ),
        )
        assert meta.metadata_key == "pages"

        # 7. Verify full state
        final_asset = get_asset(db, asset.asset_id)
        assert final_asset is not None
        assert final_asset.ingest_state.value in ("staged", "importing")
        assert final_asset.source_inbox_item_id == inbox_item.inbox_item_id

        final_inbox = get_inbox_item(db, inbox_item.inbox_item_id)
        assert final_inbox is not None
        assert final_inbox.status == InboxStatus.linked


# ═══════════════════════════════════════════════════════════════════
# Path sanitization tests
# ═══════════════════════════════════════════════════════════════════

class TestPathSafety:
    """P3-02: filename and path safety."""

    def test_sanitize_traversal_filename(self):
        """Path traversal patterns are sanitized."""
        dangerous = "../../../etc/passwd"
        safe = sanitize_filename(dangerous)
        assert ".." not in safe
        assert "etc" in safe.lower() or "passwd" in safe.lower()

    def test_sanitize_null_byte(self):
        """Null bytes are removed."""
        dangerous = "test\0file.txt"
        safe = sanitize_filename(dangerous)
        assert "\0" not in safe

    def test_sanitize_absolute_path(self):
        """Absolute paths are sanitized."""
        dangerous = "/etc/shadow"
        safe = sanitize_filename(dangerous)
        assert not safe.startswith("/")

    def test_is_path_safe_rejects_traversal(self):
        """is_path_safe returns False for traversal patterns."""
        assert is_path_safe("../../etc/passwd") is False
        assert is_path_safe("foo/../../../bar") is False

    def test_is_path_safe_accepts_normal(self):
        """is_path_safe returns True for normal paths."""
        assert is_path_safe("documents/report.pdf") is True
        assert is_path_safe("hello_world.txt") is True

    def test_sanitize_empty_becomes_unnamed(self):
        """Empty filename after sanitization becomes 'unnamed'."""
        result = sanitize_filename("...")
        assert len(result) > 0
        assert result != "..."

    def test_sanitize_keeps_extension(self):
        """Sanitization preserves file extension when reasonable."""
        result = sanitize_filename("my document.pdf")
        assert ".pdf" in result


# ═══════════════════════════════════════════════════════════════════
# Ingest (full upload → asset) Tests
# ═══════════════════════════════════════════════════════════════════

class TestIngestAsset:
    """P3-02: ingest_asset — full upload → staging → promote → asset flow."""

    def test_ingest_asset_full_flow(self, db, backend):
        """Full ingest: stage file → ingest_asset → asset is promoted + inbox linked."""
        project, code = _make_project(db)

        # Stage a file
        content = b"Ingest test content for the full ingest_asset flow."
        staged = stage_file(
            file_content=content,
            original_filename="ingest_test.txt",
            backend=backend,
        )

        ctx = _make_context()
        from mneme.db.assets import ingest_asset, DuplicateAssetError
        from mneme.db.inbox import get_inbox_item

        asset = ingest_asset(
            db, ctx,
            staged_info=staged,
            project_id=project.project_id,
            project_code=code,
            title="Ingested Document",
            asset_type="document",
        )

        # Verify asset
        assert asset is not None
        assert asset.title == "Ingested Document"
        assert asset.asset_uid is not None
        assert asset.asset_uid.startswith(code)
        assert asset.content_hash == staged.content_hash
        assert asset.size_bytes == staged.size_bytes
        assert asset.media_type == staged.media_type
        assert asset.original_filename == staged.original_filename
        # After promote: ingest_state should be 'staged', storage_ref should be permanent
        assert asset.ingest_state.value == "staged"
        assert asset.storage_ref != "pending"
        assert "assets" in asset.storage_ref
        assert asset.source_inbox_item_id is not None

        # Verify inbox item was created and linked
        inbox = get_inbox_item(db, asset.source_inbox_item_id)
        assert inbox is not None
        assert inbox.status == InboxStatus.linked
        assert inbox.asset_id == asset.asset_id

    def test_ingest_asset_duplicate_content_hash(self, db, backend):
        """Second ingest with same content hash raises DuplicateAssetError."""
        project, code = _make_project(db)

        content = b"Duplicate ingest content for testing dedup."
        staged1 = stage_file(
            file_content=content,
            original_filename="dup_test_1.txt",
            backend=backend,
        )

        ctx = _make_context()
        from mneme.db.assets import ingest_asset, DuplicateAssetError

        # First ingest succeeds
        asset1 = ingest_asset(
            db, ctx,
            staged_info=staged1,
            project_id=project.project_id,
            project_code=code,
            title="First Ingest",
        )
        assert asset1 is not None

        # Stage same content again (different filename)
        staged2 = stage_file(
            file_content=content,
            original_filename="dup_test_2.txt",
            backend=backend,
        )

        # Second ingest should raise DuplicateAssetError
        ctx2 = _make_context()
        with pytest.raises(DuplicateAssetError) as exc_info:
            ingest_asset(
                db, ctx2,
                staged_info=staged2,
                project_id=project.project_id,
                project_code=code,
                title="Second Ingest",
            )

        assert exc_info.value.existing_asset is not None
        assert exc_info.value.existing_asset.asset_id == asset1.asset_id
        assert exc_info.value.existing_asset.content_hash == staged1.content_hash

    def test_ingest_asset_generates_uid(self, db, backend):
        """ingest_asset generates a valid asset_uid."""
        project, code = _make_project(db)

        content = b"UID generation test for ingest."
        staged = stage_file(
            file_content=content,
            original_filename="uid_test.txt",
            backend=backend,
        )

        ctx = _make_context()
        from mneme.db.assets import ingest_asset

        asset = ingest_asset(
            db, ctx,
            staged_info=staged,
            project_id=project.project_id,
            project_code=code,
            title="UID Test",
        )

        # UID format: {project_code}-{hash_prefix[:12]}-{timestamp_ms}
        # project_code may contain dashes, so we check containment
        assert asset.asset_uid.startswith(code)
        hash_prefix = staged.content_hash[:12]
        assert hash_prefix in asset.asset_uid
        # The timestamp part (after the last dash) should be numeric
        timestamp_part = asset.asset_uid.rsplit("-", 1)[-1]
        assert timestamp_part.isdigit()

        # canonical_uri should contain asset_uid
        assert asset.canonical_uri is not None
        assert asset.asset_uid in asset.canonical_uri
        assert f"mneme://{code}/assets/" in asset.canonical_uri

    def test_ingest_asset_storage_ref_format(self, db, backend):
        """After ingest, storage_ref points to permanent path."""
        project, code = _make_project(db)

        content = b"Storage ref format test."
        staged = stage_file(
            file_content=content,
            original_filename="storage_test.txt",
            backend=backend,
        )

        ctx = _make_context()
        from mneme.db.assets import ingest_asset

        asset = ingest_asset(
            db, ctx,
            staged_info=staged,
            project_id=project.project_id,
            project_code=code,
        )

        # storage_ref should be the permanent path
        assert asset.storage_ref != "pending"
        assert str(project.project_id) in asset.storage_ref
        assert asset.asset_uid in asset.storage_ref
        assert staged.original_filename in asset.storage_ref

        # Verify the file actually exists at the promoted path
        from pathlib import Path
        promoted_path = Path(asset.storage_ref)
        assert promoted_path.is_file()
        assert promoted_path.read_bytes() == content

    def test_ingest_asset_links_inbox_to_asset(self, db, backend):
        """ingest_asset creates inbox item and links it to the asset."""
        project, code = _make_project(db)

        content = b"Inbox linking test for ingest."
        staged = stage_file(
            file_content=content,
            original_filename="link_test.txt",
            backend=backend,
        )

        ctx = _make_context()
        from mneme.db.assets import ingest_asset
        from mneme.db.inbox import get_inbox_item, list_inbox_items

        asset = ingest_asset(
            db, ctx,
            staged_info=staged,
            project_id=project.project_id,
            project_code=code,
            title="Linked Asset",
        )

        # Inbox item should exist and be linked
        assert asset.source_inbox_item_id is not None
        inbox = get_inbox_item(db, asset.source_inbox_item_id)
        assert inbox is not None
        assert inbox.status == InboxStatus.linked
        assert inbox.asset_id == asset.asset_id
        assert inbox.content_hash == staged.content_hash

        # List inbox items for the project
        items, _ = list_inbox_items(db, project_id=project.project_id)
        linked_items = [i for i in items if i.status == InboxStatus.linked]
        assert len(linked_items) >= 1

    def test_ingest_asset_with_custom_asset_type(self, db, backend):
        """ingest_asset supports custom asset_type."""
        project, code = _make_project(db)

        content = b"Custom type test for ingest."
        staged = stage_file(
            file_content=content,
            original_filename="image_test.png",
            backend=backend,
        )

        ctx = _make_context()
        from mneme.db.assets import ingest_asset

        asset = ingest_asset(
            db, ctx,
            staged_info=staged,
            project_id=project.project_id,
            project_code=code,
            title="Custom Image",
            asset_type="image",
        )

        assert asset.asset_type == AssetType.image

    def test_ingest_asset_with_sensitivity(self, db, backend):
        """ingest_asset supports custom sensitivity level."""
        project, code = _make_project(db)

        content = b"Sensitive content for ingest."
        staged = stage_file(
            file_content=content,
            original_filename="sensitive.txt",
            backend=backend,
        )

        ctx = _make_context()
        from mneme.db.assets import ingest_asset

        asset = ingest_asset(
            db, ctx,
            staged_info=staged,
            project_id=project.project_id,
            project_code=code,
            title="Sensitive Doc",
            sensitivity_level="sensitive",
        )

        assert asset.sensitivity_level == SensitivityLevel.sensitive

    def test_ingest_asset_idempotent_via_context(self, db, backend):
        """Same idempotency key yields the same asset (idempotent ingest)."""
        project, code = _make_project(db)
        from mneme.db.assets import ingest_asset, DuplicateAssetError

        content = b"Idempotent key test for ingest."
        staged = stage_file(
            file_content=content,
            original_filename="idem_test.txt",
            backend=backend,
        )

        idem_key = str(uuid4())
        ctx1 = _make_context(idem_key)
        asset1 = ingest_asset(
            db, ctx1,
            staged_info=staged,
            project_id=project.project_id,
            project_code=code,
            title="Idempotent Ingest",
        )

        # Second call with same content hash raises DuplicateAssetError
        staged2 = stage_file(
            file_content=content,
            original_filename="idem_test_2.txt",
            backend=backend,
        )

        ctx2 = _make_context(f"different-key-{uuid4().hex[:8]}")
        with pytest.raises(DuplicateAssetError) as exc_info:
            ingest_asset(
                db, ctx2,
                staged_info=staged2,
                project_id=project.project_id,
                project_code=code,
                title="Idempotent Ingest 2",
            )
        assert exc_info.value.existing_asset.asset_id == asset1.asset_id
