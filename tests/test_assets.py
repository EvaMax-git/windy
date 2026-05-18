"""Contract tests for Asset DB layer (P3-03).

Tests the ``mneme.db.assets`` module covering:
* create_asset — asset creation with audit/outbox/object_registry
* get_asset / get_asset_by_uid — single asset lookups
* list_assets — paginated listing with filters
* update_asset — partial field updates, sensitivity guard
* archive_asset — soft delete
* promote_from_staging — storage_ref update + ingest_state advance
* advance_ingest_state — ingest state machine
* add_metadata / list_metadata — metadata CRUD with cache sync
* lookup_asset_by_hash — content-hash dedup
"""

from __future__ import annotations

import uuid

import pytest

from mneme.api.context import ActorContext, RequestContext
from mneme.db.assets import (
    _can_transition_ingest,
    _can_transition_status,
    add_metadata,
    advance_ingest_state,
    archive_asset,
    change_asset_status,
    create_asset,
    delete_metadata,
    get_asset,
    get_asset_by_uid,
    get_metadata_by_id,
    get_metadata_by_key,
    list_assets,
    list_metadata,
    lookup_asset_by_hash,
    promote_from_staging,
    restore_asset,
    update_asset,
    update_metadata,
)
from mneme.db.projects import create_project
from mneme.schemas.storage import (
    AssetCreateRequest,
    AssetMetadataCreateRequest,
    AssetMetadataRead,
    AssetMetadataUpdateRequest,
    AssetRead,
    AssetType,
    MetadataValueType,
    RetentionPolicy,
)
from mneme.schemas.projects import ProjectCreateRequest
from mneme.schemas.common import SensitivityLevel
from tests.conftest import TEST_USER_ID


def _make_context(idem_key: str | None = None) -> RequestContext:
    """Build a minimal RequestContext for tests."""
    req_id = uuid.uuid4()
    if idem_key is None:
        idem_key = str(uuid.uuid4())
    return RequestContext(
        request_id=req_id,
        correlation_id=req_id,
        actor=ActorContext(actor_type="user", actor_id=TEST_USER_ID),
        idempotency_key=idem_key,
    )


def _make_project(db) -> tuple:
    """Create a test project and return (project_id, project_code)."""
    ctx = _make_context()
    code = f"ap{uuid.uuid4().hex[:8]}"
    payload = ProjectCreateRequest(
        project_code=code,
        name=f"Test Project {code[:8]}",
        description="Test project for asset tests",
        sensitivity_default=SensitivityLevel.normal,
    )
    project = create_project(db, ctx, payload=payload)
    return project.project_id, code


# ═══════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture
def project_id(db):
    """Create a test project and return its UUID."""
    pid, _ = _make_project(db)
    return pid


@pytest.fixture
def asset_payload(project_id):
    """Return a basic AssetCreateRequest."""
    return AssetCreateRequest(
        project_id=project_id,
        title="Test Document",
        asset_type=AssetType.document,
        media_type="application/pdf",
        original_filename="test.pdf",
        content_hash=f"sha256-{uuid.uuid4().hex[:32]}",
        size_bytes=1024,
        sensitivity_level=SensitivityLevel.normal,
        retention_policy=RetentionPolicy.default,
    )


# ═══════════════════════════════════════════════════════════════════
# create_asset
# ═══════════════════════════════════════════════════════════════════


class TestCreateAsset:
    def test_create_basic(self, db, project_id, asset_payload):
        """Create a simple asset and verify all fields."""
        ctx = _make_context()
        asset = create_asset(db, ctx, payload=asset_payload)

        assert isinstance(asset, AssetRead)
        assert asset.title == "Test Document"
        assert asset.ingest_state.value == "pending"
        assert asset.knowledge_state.value == "not_started"
        assert asset.status.value == "active"
        assert asset.storage_ref == "pending"
        assert asset.content_hash is not None
        assert asset.asset_uid is not None
        assert asset.canonical_uri is not None

    def test_asset_uid_format(self, db, project_id, asset_payload):
        """Asset UID follows {project_code}-{hash_prefix[:12]}-{timestamp_ms}."""
        ctx = _make_context()
        asset = create_asset(db, ctx, payload=asset_payload)

        parts = asset.asset_uid.split("-")
        assert len(parts) >= 3
        hash_part = asset.content_hash[:12]
        assert hash_part in asset.asset_uid

    def test_canonical_uri_format(self, db, project_id, asset_payload):
        """Canonical URI follows mneme://{project_code}/assets/{asset_uid}."""
        ctx = _make_context()
        asset = create_asset(db, ctx, payload=asset_payload)

        assert asset.canonical_uri is not None
        assert asset.canonical_uri.startswith("mneme://")
        assert "/assets/" in asset.canonical_uri
        assert asset.asset_uid in asset.canonical_uri

    def test_create_idempotent(self, db, project_id, asset_payload):
        """Same idempotency key returns the existing asset."""
        ctx = _make_context(idem_key="idem-create-001")
        a1 = create_asset(db, ctx, payload=asset_payload)
        a2 = create_asset(db, ctx, payload=asset_payload)

        assert a1.asset_id == a2.asset_id
        assert a1.asset_uid == a2.asset_uid

    def test_create_with_custom_sensitivity(self, db, project_id, asset_payload):
        """Asset can be created with elevated sensitivity."""
        asset_payload.sensitivity_level = SensitivityLevel.sensitive
        ctx = _make_context()
        asset = create_asset(db, ctx, payload=asset_payload)

        assert asset.sensitivity_level.value == "sensitive"


# ═══════════════════════════════════════════════════════════════════
# get_asset / get_asset_by_uid
# ═══════════════════════════════════════════════════════════════════


class TestGetAsset:
    def test_get_existing(self, db, project_id, asset_payload):
        """Can retrieve an asset by primary key."""
        ctx = _make_context()
        created = create_asset(db, ctx, payload=asset_payload)

        fetched = get_asset(db, created.asset_id)
        assert fetched is not None
        assert fetched.asset_id == created.asset_id

    def test_get_missing(self, db):
        """Returns None for non-existent asset."""
        result = get_asset(db, uuid.uuid4())
        assert result is None

    def test_get_by_uid(self, db, project_id, asset_payload):
        """Can retrieve by (project_id, asset_uid)."""
        ctx = _make_context()
        created = create_asset(db, ctx, payload=asset_payload)

        fetched = get_asset_by_uid(db, project_id, created.asset_uid)
        assert fetched is not None
        assert fetched.asset_id == created.asset_id

    def test_get_by_uid_wrong_project(self, db, project_id, asset_payload):
        """Returns None if project_id doesn't match."""
        ctx = _make_context()
        created = create_asset(db, ctx, payload=asset_payload)

        other_pid, _ = _make_project(db)
        fetched = get_asset_by_uid(db, other_pid, created.asset_uid)
        assert fetched is None


# ═══════════════════════════════════════════════════════════════════
# list_assets
# ═══════════════════════════════════════════════════════════════════


class TestListAssets:
    def test_list_empty(self, db, project_id):
        """Listing with no assets returns empty."""
        items, total = list_assets(db)
        assert isinstance(items, list)

    def test_list_with_multiple(self, db, project_id):
        """Can list multiple created assets."""
        for i in range(5):
            ctx = _make_context()
            payload = AssetCreateRequest(
                project_id=project_id,
                title=f"Asset {i}",
                asset_type=AssetType.document,
                content_hash=f"sha256-{uuid.uuid4().hex[:32]}",
            )
            create_asset(db, ctx, payload=payload)

        items, total = list_assets(db, project_id=project_id)
        assert total >= 5
        assert len(items) >= 5

    def test_list_pagination(self, db, project_id):
        """Pagination works correctly."""
        for i in range(10):
            ctx = _make_context()
            payload = AssetCreateRequest(
                project_id=project_id,
                title=f"Paged Asset {i}",
                asset_type=AssetType.document,
                content_hash=f"sha256-{uuid.uuid4().hex[:32]}",
            )
            create_asset(db, ctx, payload=payload)

        page1, total = list_assets(db, project_id=project_id, page=1, page_size=3)
        assert len(page1) == 3
        assert total >= 10

        page2, _ = list_assets(db, project_id=project_id, page=2, page_size=3)
        ids_p1 = {a.asset_id for a in page1}
        ids_p2 = {a.asset_id for a in page2}
        assert ids_p1.isdisjoint(ids_p2)

    def test_list_filter_by_asset_type(self, db, project_id):
        """Can filter by asset_type."""
        create_asset(db, _make_context(), payload=AssetCreateRequest(
            project_id=project_id, title="Doc", asset_type=AssetType.document,
            content_hash=f"sha256-{uuid.uuid4().hex[:32]}",
        ))
        create_asset(db, _make_context(), payload=AssetCreateRequest(
            project_id=project_id, title="Img", asset_type=AssetType.image,
            content_hash=f"sha256-{uuid.uuid4().hex[:32]}",
        ))

        items, total = list_assets(
            db, project_id=project_id, asset_type="image"
        )
        assert total >= 1
        assert all(a.asset_type.value == "image" for a in items)

    def test_list_filter_by_ingest_state(self, db, project_id):
        """Can filter by ingest_state."""
        ctx = _make_context()
        create_asset(db, ctx, payload=AssetCreateRequest(
            project_id=project_id, title="Pending",
            asset_type=AssetType.document,
            content_hash=f"sha256-{uuid.uuid4().hex[:32]}",
        ))

        items, _ = list_assets(
            db, project_id=project_id, ingest_state="pending"
        )
        assert len(items) >= 1
        assert all(a.ingest_state.value == "pending" for a in items)

    def test_list_filter_by_sensitivity(self, db, project_id):
        """Can filter by sensitivity_level."""
        ctx = _make_context()
        create_asset(db, ctx, payload=AssetCreateRequest(
            project_id=project_id, title="Secret Doc",
            asset_type=AssetType.document,
            content_hash=f"sha256-{uuid.uuid4().hex[:32]}",
            sensitivity_level=SensitivityLevel.secret,
        ))

        items, _ = list_assets(
            db, project_id=project_id, sensitivity_level="secret"
        )
        assert len(items) >= 1


# ═══════════════════════════════════════════════════════════════════
# update_asset
# ═══════════════════════════════════════════════════════════════════


class TestUpdateAsset:
    def test_update_title(self, db, project_id, asset_payload):
        """Can update asset title."""
        ctx = _make_context()
        created = create_asset(db, ctx, payload=asset_payload)

        ctx2 = _make_context()
        updated = update_asset(
            db, ctx2, asset_id=created.asset_id, title="New Title"
        )
        assert updated.title == "New Title"
        assert updated.current_version > created.current_version

    def test_update_sensitivity_raise(self, db, project_id, asset_payload):
        """Can raise sensitivity level."""
        ctx = _make_context()
        created = create_asset(db, ctx, payload=asset_payload)

        ctx2 = _make_context()
        updated = update_asset(
            db, ctx2, asset_id=created.asset_id,
            sensitivity_level="sensitive",
        )
        assert updated.sensitivity_level.value == "sensitive"

    def test_update_sensitivity_lower_raises(self, db, project_id, asset_payload):
        """Cannot lower sensitivity level."""
        asset_payload.sensitivity_level = SensitivityLevel.sensitive
        ctx = _make_context()
        created = create_asset(db, ctx, payload=asset_payload)

        ctx2 = _make_context()
        with pytest.raises(ValueError, match="Cannot lower sensitivity"):
            update_asset(
                db, ctx2, asset_id=created.asset_id,
                sensitivity_level="normal",
            )

    def test_update_retention_policy(self, db, project_id, asset_payload):
        """Can update retention policy."""
        ctx = _make_context()
        created = create_asset(db, ctx, payload=asset_payload)

        ctx2 = _make_context()
        updated = update_asset(
            db, ctx2, asset_id=created.asset_id,
            retention_policy="permanent",
        )
        assert updated.retention_policy.value == "permanent"

    def test_update_version_increment(self, db, project_id, asset_payload):
        """current_version increments on each update."""
        ctx = _make_context()
        created = create_asset(db, ctx, payload=asset_payload)
        v1 = created.current_version

        ctx2 = _make_context()
        updated1 = update_asset(db, ctx2, asset_id=created.asset_id, title="V2")
        assert updated1.current_version == v1 + 1

        ctx3 = _make_context()
        updated2 = update_asset(db, ctx3, asset_id=created.asset_id, title="V3")
        assert updated2.current_version == v1 + 2

    def test_update_missing_asset_raises(self, db):
        """Updating non-existent asset raises ValueError."""
        ctx = _make_context()
        with pytest.raises(ValueError, match="not found"):
            update_asset(db, ctx, asset_id=uuid.uuid4(), title="Nope")


# ═══════════════════════════════════════════════════════════════════
# archive_asset (soft delete)
# ═══════════════════════════════════════════════════════════════════


class TestArchiveAsset:
    def test_soft_delete(self, db, project_id, asset_payload):
        """Can soft-delete an asset."""
        ctx = _make_context()
        created = create_asset(db, ctx, payload=asset_payload)
        assert created.status.value == "active"

        ctx2 = _make_context()
        archived = archive_asset(db, ctx2, asset_id=created.asset_id)
        assert archived.status.value == "deleted"
        assert archived.archived_at is not None

    def test_cannot_archive_twice(self, db, project_id, asset_payload):
        """Archiving an already archived asset raises error."""
        ctx = _make_context()
        created = create_asset(db, ctx, payload=asset_payload)
        ctx2 = _make_context()
        archive_asset(db, ctx2, asset_id=created.asset_id)

        ctx3 = _make_context()
        with pytest.raises(ValueError, match="Invalid status transition"):
            archive_asset(db, ctx3, asset_id=created.asset_id)

    def test_cannot_archive_missing(self, db):
        """Archiving non-existent asset raises ValueError."""
        ctx = _make_context()
        with pytest.raises(ValueError, match="not found"):
            archive_asset(db, ctx, asset_id=uuid.uuid4())


# ═══════════════════════════════════════════════════════════════════
# promote_from_staging
# ═══════════════════════════════════════════════════════════════════


class TestPromoteFromStaging:
    def test_promote_updates_storage_ref(self, db, project_id, asset_payload):
        """Promote sets storage_ref and advances ingest_state to 'staged'."""
        ctx = _make_context()
        created = create_asset(db, ctx, payload=asset_payload)
        assert created.ingest_state.value == "pending"
        assert created.storage_ref == "pending"

        ctx2 = _make_context()
        new_ref = f"/data/assets/{project_id}/{created.asset_uid}/test.pdf"
        promoted = promote_from_staging(
            db, ctx2,
            asset_id=created.asset_id,
            storage_ref=new_ref,
            size_bytes=2048,
        )
        assert promoted.storage_ref == new_ref
        assert promoted.ingest_state.value == "staged"
        assert promoted.size_bytes == 2048

    def test_promote_invalid_state(self, db, project_id, asset_payload):
        """Cannot promote an asset that is not in 'pending' ingest_state."""
        ctx = _make_context()
        created = create_asset(db, ctx, payload=asset_payload)

        ctx2 = _make_context()
        new_ref = f"/data/assets/{project_id}/{created.asset_uid}/test.pdf"
        promote_from_staging(db, ctx2, asset_id=created.asset_id, storage_ref=new_ref)

        ctx3 = _make_context()
        with pytest.raises(ValueError, match="Invalid ingest state transition"):
            promote_from_staging(db, ctx3, asset_id=created.asset_id, storage_ref=new_ref)


# ═══════════════════════════════════════════════════════════════════
# advance_ingest_state
# ═══════════════════════════════════════════════════════════════════


class TestAdvanceIngestState:
    def test_advance_pending_to_staged(self, db, project_id, asset_payload):
        """Can advance from pending to staged."""
        ctx = _make_context()
        created = create_asset(db, ctx, payload=asset_payload)

        ctx2 = _make_context()
        advanced = advance_ingest_state(
            db, ctx2,
            asset_id=created.asset_id,
            expected_ingest_state="pending",
            new_ingest_state="staged",
        )
        assert advanced.ingest_state.value == "staged"

    def test_advance_importing_to_ready(self, db, project_id, asset_payload):
        """Can advance the full chain: pending → staged → importing → ready."""
        ctx = _make_context()
        created = create_asset(db, ctx, payload=asset_payload)

        ctx2 = _make_context()
        advance_ingest_state(
            db, ctx2,
            asset_id=created.asset_id,
            expected_ingest_state="pending",
            new_ingest_state="staged",
        )
        ctx3 = _make_context()
        advance_ingest_state(
            db, ctx3,
            asset_id=created.asset_id,
            expected_ingest_state="staged",
            new_ingest_state="importing",
        )
        ctx4 = _make_context()
        ready = advance_ingest_state(
            db, ctx4,
            asset_id=created.asset_id,
            expected_ingest_state="importing",
            new_ingest_state="ready",
        )
        assert ready.ingest_state.value == "ready"

    def test_advance_wrong_expected_state(self, db, project_id, asset_payload):
        """Fails if expected state doesn't match."""
        ctx = _make_context()
        created = create_asset(db, ctx, payload=asset_payload)

        ctx2 = _make_context()
        with pytest.raises(ValueError, match="Expected ingest_state"):
            advance_ingest_state(
                db, ctx2,
                asset_id=created.asset_id,
                expected_ingest_state="staged",
                new_ingest_state="importing",
            )

    def test_advance_invalid_transition(self, db, project_id, asset_payload):
        """Cannot skip states (e.g. pending → ready)."""
        ctx = _make_context()
        created = create_asset(db, ctx, payload=asset_payload)

        ctx2 = _make_context()
        with pytest.raises(ValueError, match="Invalid ingest state transition"):
            advance_ingest_state(
                db, ctx2,
                asset_id=created.asset_id,
                expected_ingest_state="pending",
                new_ingest_state="ready",
            )

    def test_advance_to_failed(self, db, project_id, asset_payload):
        """Can advance to failed from any valid state."""
        ctx = _make_context()
        created = create_asset(db, ctx, payload=asset_payload)

        ctx2 = _make_context()
        failed = advance_ingest_state(
            db, ctx2,
            asset_id=created.asset_id,
            expected_ingest_state="pending",
            new_ingest_state="failed",
        )
        assert failed.ingest_state.value == "failed"

    def test_retry_from_failed(self, db, project_id, asset_payload):
        """Can retry from failed back to pending."""
        ctx = _make_context()
        created = create_asset(db, ctx, payload=asset_payload)

        ctx2 = _make_context()
        advance_ingest_state(
            db, ctx2, asset_id=created.asset_id,
            expected_ingest_state="pending", new_ingest_state="failed",
        )
        ctx3 = _make_context()
        retried = advance_ingest_state(
            db, ctx3, asset_id=created.asset_id,
            expected_ingest_state="failed", new_ingest_state="pending",
        )
        assert retried.ingest_state.value == "pending"


# ═══════════════════════════════════════════════════════════════════
# Add & List Metadata
# ═══════════════════════════════════════════════════════════════════


class TestAssetMetadata:
    # ── Create (upsert) ─────────────────────────────────────────────

    def test_add_metadata(self, db, project_id, asset_payload):
        """Can add a metadata entry to an asset."""
        ctx = _make_context()
        asset = create_asset(db, ctx, payload=asset_payload)

        meta = add_metadata(
            db, ctx, asset_id=asset.asset_id,
            payload=AssetMetadataCreateRequest(
                metadata_key="author", metadata_value="Test Author",
                value_type=MetadataValueType.text, source="manual",
            ),
        )
        assert isinstance(meta, AssetMetadataRead)
        assert meta.metadata_key == "author"
        assert meta.metadata_value == "Test Author"
        assert meta.source == "manual"

    def test_add_metadata_upsert(self, db, project_id, asset_payload):
        """Re-adding same key+source updates existing entry (with fresh idempotency keys)."""
        ctx = _make_context()
        asset = create_asset(db, ctx, payload=asset_payload)

        ctx2 = _make_context()
        add_metadata(
            db, ctx2, asset_id=asset.asset_id,
            payload=AssetMetadataCreateRequest(
                metadata_key="version", metadata_value="1.0", source="manual",
            ),
        )
        ctx3 = _make_context()
        m2 = add_metadata(
            db, ctx3, asset_id=asset.asset_id,
            payload=AssetMetadataCreateRequest(
                metadata_key="version", metadata_value="2.0", source="manual",
            ),
        )
        assert m2.metadata_value == "2.0"

    def test_add_metadata_different_sources(self, db, project_id, asset_payload):
        """Same key with different sources creates separate entries."""
        ctx = _make_context()
        asset = create_asset(db, ctx, payload=asset_payload)

        add_metadata(
            db, ctx, asset_id=asset.asset_id,
            payload=AssetMetadataCreateRequest(
                metadata_key="author", metadata_value="System",
                source="system",
            ),
        )
        add_metadata(
            db, ctx, asset_id=asset.asset_id,
            payload=AssetMetadataCreateRequest(
                metadata_key="author", metadata_value="Manual",
                source="manual",
            ),
        )

        items = list_metadata(db, asset_id=asset.asset_id)
        author_items = [m for m in items if m.metadata_key == "author"]
        assert len(author_items) == 2

    def test_add_metadata_with_confidence(self, db, project_id, asset_payload):
        """Confidence value is preserved."""
        ctx = _make_context()
        asset = create_asset(db, ctx, payload=asset_payload)

        meta = add_metadata(
            db, ctx, asset_id=asset.asset_id,
            payload=AssetMetadataCreateRequest(
                metadata_key="detected_lang", metadata_value="en",
                source="system", confidence=0.95,
            ),
        )
        assert meta.confidence == 0.95

    def test_add_metadata_missing_asset_raises(self, db):
        """Adding metadata to non-existent asset raises ValueError."""
        ctx = _make_context()
        with pytest.raises(ValueError, match="not found"):
            add_metadata(
                db, ctx, asset_id=uuid.uuid4(),
                payload=AssetMetadataCreateRequest(metadata_key="k"),
            )

    # ── List ────────────────────────────────────────────────────────

    def test_list_metadata_empty(self, db, project_id, asset_payload):
        """Listing metadata for asset with none returns empty list."""
        ctx = _make_context()
        asset = create_asset(db, ctx, payload=asset_payload)

        items = list_metadata(db, asset_id=asset.asset_id)
        assert items == []

    def test_list_metadata_ordered(self, db, project_id, asset_payload):
        """Metadata entries are ordered by key."""
        ctx = _make_context()
        asset = create_asset(db, ctx, payload=asset_payload)

        keys = ["zzz", "aaa", "mmm"]
        for key in keys:
            add_metadata(
                db, ctx, asset_id=asset.asset_id,
                payload=AssetMetadataCreateRequest(
                    metadata_key=key, metadata_value="val", source="manual",
                ),
            )

        items = list_metadata(db, asset_id=asset.asset_id)
        item_keys = [m.metadata_key for m in items]
        assert item_keys == sorted(item_keys)

    # ── Get by ID ───────────────────────────────────────────────────

    def test_get_metadata_by_id(self, db, project_id, asset_payload):
        """Can retrieve a metadata entry by primary key."""
        ctx = _make_context()
        asset = create_asset(db, ctx, payload=asset_payload)

        created = add_metadata(
            db, ctx, asset_id=asset.asset_id,
            payload=AssetMetadataCreateRequest(
                metadata_key="author", metadata_value="Me", source="manual",
            ),
        )

        fetched = get_metadata_by_id(
            db, asset_metadata_id=created.asset_metadata_id
        )
        assert fetched is not None
        assert fetched.asset_metadata_id == created.asset_metadata_id
        assert fetched.metadata_key == "author"

    def test_get_metadata_by_id_missing(self, db):
        """Returns None for non-existent metadata ID."""
        result = get_metadata_by_id(db, asset_metadata_id=uuid.uuid4())
        assert result is None

    # ── Get by key ──────────────────────────────────────────────────

    def test_get_metadata_by_key(self, db, project_id, asset_payload):
        """Can retrieve a metadata entry by (asset_id, key, source)."""
        ctx = _make_context()
        asset = create_asset(db, ctx, payload=asset_payload)

        add_metadata(
            db, ctx, asset_id=asset.asset_id,
            payload=AssetMetadataCreateRequest(
                metadata_key="license", metadata_value="MIT",
                source="manual",
            ),
        )

        fetched = get_metadata_by_key(
            db, asset_id=asset.asset_id,
            metadata_key="license", source="manual",
        )
        assert fetched is not None
        assert fetched.metadata_value == "MIT"

    def test_get_metadata_by_key_missing_source(self, db, project_id, asset_payload):
        """Returns None if source doesn't match."""
        ctx = _make_context()
        asset = create_asset(db, ctx, payload=asset_payload)

        add_metadata(
            db, ctx, asset_id=asset.asset_id,
            payload=AssetMetadataCreateRequest(
                metadata_key="license", metadata_value="MIT",
                source="system",
            ),
        )

        fetched = get_metadata_by_key(
            db, asset_id=asset.asset_id,
            metadata_key="license", source="manual",
        )
        assert fetched is None

    # ── Update (PATCH) ──────────────────────────────────────────────

    def test_update_metadata_value(self, db, project_id, asset_payload):
        """Can update metadata_value via PATCH."""
        ctx = _make_context()
        asset = create_asset(db, ctx, payload=asset_payload)

        created = add_metadata(
            db, ctx, asset_id=asset.asset_id,
            payload=AssetMetadataCreateRequest(
                metadata_key="version", metadata_value="1.0", source="manual",
            ),
        )

        updated = update_metadata(
            db, ctx,
            asset_metadata_id=created.asset_metadata_id,
            asset_id=asset.asset_id,
            payload=AssetMetadataUpdateRequest(metadata_value="2.0"),
        )
        assert updated.metadata_value == "2.0"

    def test_update_metadata_confidence(self, db, project_id, asset_payload):
        """Can update confidence without touching value."""
        ctx = _make_context()
        asset = create_asset(db, ctx, payload=asset_payload)

        created = add_metadata(
            db, ctx, asset_id=asset.asset_id,
            payload=AssetMetadataCreateRequest(
                metadata_key="detected", metadata_value="en",
                source="system", confidence=0.5,
            ),
        )

        updated = update_metadata(
            db, ctx,
            asset_metadata_id=created.asset_metadata_id,
            asset_id=asset.asset_id,
            payload=AssetMetadataUpdateRequest(confidence=0.9),
        )
        assert updated.confidence == 0.9
        assert updated.metadata_value == "en"

    def test_update_metadata_value_type(self, db, project_id, asset_payload):
        """Can change value_type when value is compatible."""
        ctx = _make_context()
        asset = create_asset(db, ctx, payload=asset_payload)

        created = add_metadata(
            db, ctx, asset_id=asset.asset_id,
            payload=AssetMetadataCreateRequest(
                metadata_key="count", metadata_value="42",
                value_type=MetadataValueType.text, source="manual",
            ),
        )

        updated = update_metadata(
            db, ctx,
            asset_metadata_id=created.asset_metadata_id,
            asset_id=asset.asset_id,
            payload=AssetMetadataUpdateRequest(
                value_type=MetadataValueType.number,
            ),
        )
        assert updated.value_type == "number"

    def test_update_metadata_type_mismatch_raises(self, db, project_id, asset_payload):
        """Changing value_type to incompatible type raises ValueError."""
        ctx = _make_context()
        asset = create_asset(db, ctx, payload=asset_payload)

        created = add_metadata(
            db, ctx, asset_id=asset.asset_id,
            payload=AssetMetadataCreateRequest(
                metadata_key="title", metadata_value="hello world",
                value_type=MetadataValueType.text, source="manual",
            ),
        )

        with pytest.raises(ValueError, match="not a valid number"):
            update_metadata(
                db, ctx,
                asset_metadata_id=created.asset_metadata_id,
                asset_id=asset.asset_id,
                payload=AssetMetadataUpdateRequest(
                    value_type=MetadataValueType.number,
                ),
            )

    def test_update_metadata_wrong_asset_raises(self, db, project_id, asset_payload):
        """Updating metadata with mismatched asset_id raises."""
        ctx = _make_context()
        asset = create_asset(db, ctx, payload=asset_payload)

        created = add_metadata(
            db, ctx, asset_id=asset.asset_id,
            payload=AssetMetadataCreateRequest(metadata_key="k", source="manual"),
        )

        # Create a second asset
        ctx2 = _make_context()
        payload2 = AssetCreateRequest(
            project_id=project_id,
            title="Asset 2",
            asset_type=AssetType.document,
            content_hash=f"sha256-{uuid.uuid4().hex[:32]}",
        )
        asset2 = create_asset(db, ctx2, payload=payload2)

        with pytest.raises(ValueError, match="does not belong"):
            update_metadata(
                db, ctx,
                asset_metadata_id=created.asset_metadata_id,
                asset_id=asset2.asset_id,
                payload=AssetMetadataUpdateRequest(metadata_value="bad"),
            )

    def test_update_metadata_missing_raises(self, db):
        """Updating non-existent metadata raises ValueError."""
        ctx = _make_context()
        with pytest.raises(ValueError, match="not found"):
            update_metadata(
                db, ctx,
                asset_metadata_id=uuid.uuid4(),
                asset_id=uuid.uuid4(),
                payload=AssetMetadataUpdateRequest(metadata_value="nope"),
            )

    # ── Delete ──────────────────────────────────────────────────────

    def test_delete_metadata(self, db, project_id, asset_payload):
        """Can delete a metadata entry."""
        ctx = _make_context()
        asset = create_asset(db, ctx, payload=asset_payload)

        created = add_metadata(
            db, ctx, asset_id=asset.asset_id,
            payload=AssetMetadataCreateRequest(
                metadata_key="temp", metadata_value="delete-me",
                source="manual",
            ),
        )

        deleted = delete_metadata(
            db, ctx,
            asset_metadata_id=created.asset_metadata_id,
            asset_id=asset.asset_id,
        )
        assert deleted.asset_metadata_id == created.asset_metadata_id
        assert deleted.metadata_key == "temp"

        # Verify gone
        assert get_metadata_by_id(
            db, asset_metadata_id=created.asset_metadata_id
        ) is None

    def test_delete_metadata_updates_cache(self, db, project_id, asset_payload):
        """Deleting metadata rebuilds the assets.metadata_json cache."""
        ctx = _make_context()
        asset = create_asset(db, ctx, payload=asset_payload)

        created = add_metadata(
            db, ctx, asset_id=asset.asset_id,
            payload=AssetMetadataCreateRequest(
                metadata_key="cache_key", metadata_value="cache_val",
                source="manual",
            ),
        )

        delete_metadata(
            db, ctx,
            asset_metadata_id=created.asset_metadata_id,
            asset_id=asset.asset_id,
        )

        items = list_metadata(db, asset_id=asset.asset_id)
        assert len([m for m in items if m.metadata_key == "cache_key"]) == 0

    def test_delete_metadata_twice_raises(self, db, project_id, asset_payload):
        """Deleting already-deleted metadata raises ValueError."""
        ctx = _make_context()
        asset = create_asset(db, ctx, payload=asset_payload)

        created = add_metadata(
            db, ctx, asset_id=asset.asset_id,
            payload=AssetMetadataCreateRequest(metadata_key="k", source="manual"),
        )

        delete_metadata(
            db, ctx,
            asset_metadata_id=created.asset_metadata_id,
            asset_id=asset.asset_id,
        )

        with pytest.raises(ValueError, match="not found"):
            delete_metadata(
                db, ctx,
                asset_metadata_id=created.asset_metadata_id,
                asset_id=asset.asset_id,
            )

    def test_delete_metadata_wrong_asset_raises(self, db, project_id, asset_payload):
        """Deleting metadata with mismatched asset_id raises."""
        ctx = _make_context()
        asset = create_asset(db, ctx, payload=asset_payload)

        created = add_metadata(
            db, ctx, asset_id=asset.asset_id,
            payload=AssetMetadataCreateRequest(metadata_key="k", source="manual"),
        )

        ctx2 = _make_context()
        payload2 = AssetCreateRequest(
            project_id=project_id,
            title="Asset 2",
            asset_type=AssetType.document,
            content_hash=f"sha256-{uuid.uuid4().hex[:32]}",
        )
        asset2 = create_asset(db, ctx2, payload=payload2)

        with pytest.raises(ValueError, match="does not belong"):
            delete_metadata(
                db, ctx,
                asset_metadata_id=created.asset_metadata_id,
                asset_id=asset2.asset_id,
            )

    # ── Unique constraint handling ──────────────────────────────────

    def test_upsert_same_key_same_source_atomic(self, db, project_id, asset_payload):
        """Upsert with same (key, source) replaces existing atomically.

        Each logical write must use a *different* idempotency key so the
        second call is treated as an actual upsert, not an idempotent replay.
        """
        ctx = _make_context()
        asset = create_asset(db, ctx, payload=asset_payload)

        ctx1 = _make_context()
        m1 = add_metadata(
            db, ctx1, asset_id=asset.asset_id,
            payload=AssetMetadataCreateRequest(
                metadata_key="author", metadata_value="Alice",
                source="manual",
            ),
        )
        # Same key+source but different idempotency key — should update
        ctx2 = _make_context()
        m2 = add_metadata(
            db, ctx2, asset_id=asset.asset_id,
            payload=AssetMetadataCreateRequest(
                metadata_key="author", metadata_value="Bob",
                source="manual",
            ),
        )
        assert m2.metadata_value == "Bob"

        # Only one row with (asset_id, key, source)
        items = list_metadata(db, asset_id=asset.asset_id)
        author_items = [
            m for m in items
            if m.metadata_key == "author" and m.source == "manual"
        ]
        assert len(author_items) == 1

    def test_upsert_preserves_created_at(self, db, project_id, asset_payload):
        """Upsert should update the row but preserve original created_at."""
        ctx = _make_context()
        asset = create_asset(db, ctx, payload=asset_payload)

        m1 = add_metadata(
            db, ctx, asset_id=asset.asset_id,
            payload=AssetMetadataCreateRequest(
                metadata_key="persistent", metadata_value="v1",
                source="manual",
            ),
        )

        m2 = add_metadata(
            db, ctx, asset_id=asset.asset_id,
            payload=AssetMetadataCreateRequest(
                metadata_key="persistent", metadata_value="v2",
                source="manual",
            ),
        )

        # created_at should not change on upsert
        assert m1.created_at == m2.created_at

    # ── value_type validation ───────────────────────────────────────

    def test_value_type_number_valid(self, db, project_id, asset_payload):
        """value_type='number' accepts valid numeric strings."""
        ctx = _make_context()
        asset = create_asset(db, ctx, payload=asset_payload)

        meta = add_metadata(
            db, ctx, asset_id=asset.asset_id,
            payload=AssetMetadataCreateRequest(
                metadata_key="score", metadata_value="98.6",
                value_type=MetadataValueType.number, source="system",
            ),
        )
        assert meta.value_type == "number"

    def test_value_type_number_integer(self, db, project_id, asset_payload):
        """value_type='number' accepts integers."""
        ctx = _make_context()
        asset = create_asset(db, ctx, payload=asset_payload)

        meta = add_metadata(
            db, ctx, asset_id=asset.asset_id,
            payload=AssetMetadataCreateRequest(
                metadata_key="count", metadata_value="42",
                value_type=MetadataValueType.number, source="system",
            ),
        )
        assert meta.value_type == "number"

    def test_value_type_number_negative(self, db, project_id, asset_payload):
        """value_type='number' accepts negative numbers."""
        ctx = _make_context()
        asset = create_asset(db, ctx, payload=asset_payload)

        meta = add_metadata(
            db, ctx, asset_id=asset.asset_id,
            payload=AssetMetadataCreateRequest(
                metadata_key="delta", metadata_value="-10.5",
                value_type=MetadataValueType.number, source="system",
            ),
        )
        assert meta.value_type == "number"

    def test_value_type_number_invalid_raises(self, db, project_id, asset_payload):
        """value_type='number' rejects non-numeric strings."""
        ctx = _make_context()
        asset = create_asset(db, ctx, payload=asset_payload)

        with pytest.raises(ValueError, match="not a valid number"):
            add_metadata(
                db, ctx, asset_id=asset.asset_id,
                payload=AssetMetadataCreateRequest(
                    metadata_key="score", metadata_value="high",
                    value_type=MetadataValueType.number, source="system",
                ),
            )

    def test_value_type_boolean_true(self, db, project_id, asset_payload):
        """value_type='boolean' accepts 'true'."""
        ctx = _make_context()
        asset = create_asset(db, ctx, payload=asset_payload)

        meta = add_metadata(
            db, ctx, asset_id=asset.asset_id,
            payload=AssetMetadataCreateRequest(
                metadata_key="active", metadata_value="true",
                value_type=MetadataValueType.boolean, source="system",
            ),
        )
        assert meta.value_type == "boolean"

    def test_value_type_boolean_false(self, db, project_id, asset_payload):
        """value_type='boolean' accepts 'false'."""
        ctx = _make_context()
        asset = create_asset(db, ctx, payload=asset_payload)

        meta = add_metadata(
            db, ctx, asset_id=asset.asset_id,
            payload=AssetMetadataCreateRequest(
                metadata_key="active", metadata_value="False",
                value_type=MetadataValueType.boolean, source="system",
            ),
        )
        assert meta.value_type == "boolean"

    def test_value_type_boolean_1_0(self, db, project_id, asset_payload):
        """value_type='boolean' accepts '1' and '0'."""
        ctx = _make_context()
        asset = create_asset(db, ctx, payload=asset_payload)

        m1 = add_metadata(
            db, ctx, asset_id=asset.asset_id,
            payload=AssetMetadataCreateRequest(
                metadata_key="flag_a", metadata_value="1",
                value_type=MetadataValueType.boolean, source="system",
            ),
        )
        assert m1 is not None

        m2 = add_metadata(
            db, ctx, asset_id=asset.asset_id,
            payload=AssetMetadataCreateRequest(
                metadata_key="flag_b", metadata_value="0",
                value_type=MetadataValueType.boolean, source="system",
            ),
        )
        assert m2 is not None

    def test_value_type_boolean_invalid_raises(self, db, project_id, asset_payload):
        """value_type='boolean' rejects invalid values."""
        ctx = _make_context()
        asset = create_asset(db, ctx, payload=asset_payload)

        with pytest.raises(ValueError, match="not a valid boolean"):
            add_metadata(
                db, ctx, asset_id=asset.asset_id,
                payload=AssetMetadataCreateRequest(
                    metadata_key="flag", metadata_value="maybe",
                    value_type=MetadataValueType.boolean, source="system",
                ),
            )

    def test_value_type_date_valid(self, db, project_id, asset_payload):
        """value_type='date' accepts ISO 8601 dates."""
        ctx = _make_context()
        asset = create_asset(db, ctx, payload=asset_payload)

        meta = add_metadata(
            db, ctx, asset_id=asset.asset_id,
            payload=AssetMetadataCreateRequest(
                metadata_key="published", metadata_value="2025-12-31",
                value_type=MetadataValueType.date, source="system",
            ),
        )
        assert meta.value_type == "date"

    def test_value_type_date_invalid_format_raises(self, db, project_id, asset_payload):
        """value_type='date' rejects non-ISO formats."""
        ctx = _make_context()
        asset = create_asset(db, ctx, payload=asset_payload)

        with pytest.raises(ValueError, match="not a valid date"):
            add_metadata(
                db, ctx, asset_id=asset.asset_id,
                payload=AssetMetadataCreateRequest(
                    metadata_key="published", metadata_value="12/31/2025",
                    value_type=MetadataValueType.date, source="system",
                ),
            )

    def test_value_type_date_invalid_calendar_raises(self, db, project_id, asset_payload):
        """value_type='date' rejects impossible dates like 2025-02-30."""
        ctx = _make_context()
        asset = create_asset(db, ctx, payload=asset_payload)

        with pytest.raises(ValueError, match="not a valid calendar date"):
            add_metadata(
                db, ctx, asset_id=asset.asset_id,
                payload=AssetMetadataCreateRequest(
                    metadata_key="published", metadata_value="2025-02-30",
                    value_type=MetadataValueType.date, source="system",
                ),
            )

    def test_value_type_json_valid(self, db, project_id, asset_payload):
        """value_type='json' accepts valid JSON strings."""
        ctx = _make_context()
        asset = create_asset(db, ctx, payload=asset_payload)

        meta = add_metadata(
            db, ctx, asset_id=asset.asset_id,
            payload=AssetMetadataCreateRequest(
                metadata_key="config",
                metadata_value='{"key": "value", "num": 42}',
                value_type=MetadataValueType.json, source="system",
            ),
        )
        assert meta.value_type == "json"

    def test_value_type_json_invalid_raises(self, db, project_id, asset_payload):
        """value_type='json' rejects malformed JSON."""
        ctx = _make_context()
        asset = create_asset(db, ctx, payload=asset_payload)

        with pytest.raises(ValueError, match="not valid JSON"):
            add_metadata(
                db, ctx, asset_id=asset.asset_id,
                payload=AssetMetadataCreateRequest(
                    metadata_key="config", metadata_value="{bad json",
                    value_type=MetadataValueType.json, source="system",
                ),
            )

    def test_value_type_null_value_always_allowed(self, db, project_id, asset_payload):
        """None value is always valid regardless of value_type."""
        ctx = _make_context()
        asset = create_asset(db, ctx, payload=asset_payload)

        # None should pass even with number type
        meta = add_metadata(
            db, ctx, asset_id=asset.asset_id,
            payload=AssetMetadataCreateRequest(
                metadata_key="unset", metadata_value=None,
                value_type=MetadataValueType.number, source="system",
            ),
        )
        assert meta.metadata_value is None

    def test_value_type_text_always_valid(self, db, project_id, asset_payload):
        """value_type='text' accepts any string."""
        ctx = _make_context()
        asset = create_asset(db, ctx, payload=asset_payload)

        meta = add_metadata(
            db, ctx, asset_id=asset.asset_id,
            payload=AssetMetadataCreateRequest(
                metadata_key="note", metadata_value="anything goes! @#$%",
                value_type=MetadataValueType.text, source="manual",
            ),
        )
        assert meta.metadata_value == "anything goes! @#$%"

    # ── metadata_json cache consistency ─────────────────────────────

    def test_cache_updated_on_upsert(self, db, project_id, asset_payload):
        """assets.metadata_json is rebuilt after metadata upsert."""
        ctx = _make_context()
        asset = create_asset(db, ctx, payload=asset_payload)

        add_metadata(
            db, ctx, asset_id=asset.asset_id,
            payload=AssetMetadataCreateRequest(
                metadata_key="author", metadata_value="Alice",
                source="manual",
            ),
        )

        # Verify via list_metadata (which reads from asset_metadata table)
        items = list_metadata(db, asset_id=asset.asset_id)
        assert len(items) == 1
        assert items[0].metadata_key == "author"
        assert items[0].metadata_value == "Alice"

    def test_cache_cleared_on_delete(self, db, project_id, asset_payload):
        """assets.metadata_json is rebuilt after metadata delete."""
        ctx = _make_context()
        asset = create_asset(db, ctx, payload=asset_payload)

        m1 = add_metadata(
            db, ctx, asset_id=asset.asset_id,
            payload=AssetMetadataCreateRequest(
                metadata_key="key1", metadata_value="val1",
                source="manual",
            ),
        )
        m2 = add_metadata(
            db, ctx, asset_id=asset.asset_id,
            payload=AssetMetadataCreateRequest(
                metadata_key="key2", metadata_value="val2",
                source="manual",
            ),
        )

        delete_metadata(
            db, ctx,
            asset_metadata_id=m1.asset_metadata_id,
            asset_id=asset.asset_id,
        )

        items = list_metadata(db, asset_id=asset.asset_id)
        assert len(items) == 1
        assert items[0].metadata_key == "key2"


# ═══════════════════════════════════════════════════════════════════
# lookup_asset_by_hash (content-hash dedup)
# ═══════════════════════════════════════════════════════════════════


class TestLookupAssetByHash:
    def test_lookup_existing(self, db, project_id, asset_payload):
        """Can find an asset by its content hash."""
        ctx = _make_context()
        created = create_asset(db, ctx, payload=asset_payload)

        found = lookup_asset_by_hash(
            db,
            content_hash=asset_payload.content_hash,
            project_id=project_id,
        )
        assert found is not None
        assert found.asset_id == created.asset_id

    def test_lookup_nonexistent(self, db, project_id):
        """Returns None for unknown hash."""
        found = lookup_asset_by_hash(
            db,
            content_hash="nonexistent-hash",
            project_id=project_id,
        )
        assert found is None

    def test_lookup_deleted_excluded(self, db, project_id, asset_payload):
        """Deleted assets are excluded from hash lookup."""
        ctx = _make_context()
        created = create_asset(db, ctx, payload=asset_payload)
        ctx2 = _make_context()
        archive_asset(db, ctx2, asset_id=created.asset_id)

        found = lookup_asset_by_hash(
            db,
            content_hash=asset_payload.content_hash,
            project_id=project_id,
        )
        assert found is None


# ═══════════════════════════════════════════════════════════════════
# Status state machine
# ═══════════════════════════════════════════════════════════════════


class TestStatusStateMachine:
    """Verifies that _VALID_STATUS_TRANSITIONS is enforced."""

    def test_valid_transition_active_to_archived(self, db, project_id, asset_payload):
        """active → archived is valid."""
        ctx = _make_context()
        created = create_asset(db, ctx, payload=asset_payload)

        ctx2 = _make_context()
        archived = change_asset_status(
            db, ctx2, asset_id=created.asset_id,
            new_status="archived",
        )
        assert archived.status.value == "archived"
        assert archived.archived_at is not None

    def test_valid_transition_active_to_deleted(self, db, project_id, asset_payload):
        """active → deleted is valid."""
        ctx = _make_context()
        created = create_asset(db, ctx, payload=asset_payload)

        ctx2 = _make_context()
        deleted = change_asset_status(
            db, ctx2, asset_id=created.asset_id,
            new_status="deleted",
        )
        assert deleted.status.value == "deleted"

    def test_valid_transition_active_to_quarantined(self, db, project_id, asset_payload):
        """active → quarantined is valid."""
        ctx = _make_context()
        created = create_asset(db, ctx, payload=asset_payload)

        ctx2 = _make_context()
        quarantined = change_asset_status(
            db, ctx2, asset_id=created.asset_id,
            new_status="quarantined",
        )
        assert quarantined.status.value == "quarantined"

    def test_valid_transition_archived_to_active(self, db, project_id, asset_payload):
        """archived → active is valid (restore)."""
        ctx = _make_context()
        created = create_asset(db, ctx, payload=asset_payload)

        ctx2 = _make_context()
        change_asset_status(
            db, ctx2, asset_id=created.asset_id,
            new_status="archived",
        )
        ctx3 = _make_context()
        restored = change_asset_status(
            db, ctx3, asset_id=created.asset_id,
            new_status="active",
        )
        assert restored.status.value == "active"
        assert restored.archived_at is None

    def test_valid_transition_deleted_to_active(self, db, project_id, asset_payload):
        """deleted → active is valid (restore)."""
        ctx = _make_context()
        created = create_asset(db, ctx, payload=asset_payload)

        ctx2 = _make_context()
        change_asset_status(
            db, ctx2, asset_id=created.asset_id,
            new_status="deleted",
        )
        ctx3 = _make_context()
        restored = change_asset_status(
            db, ctx3, asset_id=created.asset_id,
            new_status="active",
        )
        assert restored.status.value == "active"

    def test_valid_transition_quarantined_to_active(self, db, project_id, asset_payload):
        """quarantined → active is valid (release)."""
        ctx = _make_context()
        created = create_asset(db, ctx, payload=asset_payload)

        ctx2 = _make_context()
        change_asset_status(
            db, ctx2, asset_id=created.asset_id,
            new_status="quarantined",
        )
        ctx3 = _make_context()
        released = change_asset_status(
            db, ctx3, asset_id=created.asset_id,
            new_status="active",
        )
        assert released.status.value == "active"

    def test_invalid_transition_raises(self, db, project_id, asset_payload):
        """Invalid transitions raise ValueError."""
        ctx = _make_context()
        created = create_asset(db, ctx, payload=asset_payload)

        # archived → deleted is invalid
        ctx2 = _make_context()
        change_asset_status(
            db, ctx2, asset_id=created.asset_id,
            new_status="archived",
        )
        ctx3 = _make_context()
        with pytest.raises(ValueError, match="Invalid status transition"):
            change_asset_status(
                db, ctx3, asset_id=created.asset_id,
                new_status="deleted",
            )

    def test_expected_status_optimistic_check(self, db, project_id, asset_payload):
        """expected_status mismatch raises ValueError."""
        ctx = _make_context()
        created = create_asset(db, ctx, payload=asset_payload)

        ctx2 = _make_context()
        with pytest.raises(ValueError, match="Expected status"):
            change_asset_status(
                db, ctx2, asset_id=created.asset_id,
                new_status="deleted", expected_status="archived",
            )

    def test_can_transition_status_helper(self):
        """Unit test for _can_transition_status."""
        assert _can_transition_status("active", "deleted") is True
        assert _can_transition_status("active", "archived") is True
        assert _can_transition_status("active", "quarantined") is True
        assert _can_transition_status("deleted", "active") is True
        assert _can_transition_status("archived", "active") is True
        assert _can_transition_status("quarantined", "active") is True
        assert _can_transition_status("deleted", "archived") is False
        assert _can_transition_status("archived", "deleted") is False
        assert _can_transition_status("active", "active") is False


# ═══════════════════════════════════════════════════════════════════
# restore_asset
# ═══════════════════════════════════════════════════════════════════


class TestRestoreAsset:
    def test_restore_deleted(self, db, project_id, asset_payload):
        """Can restore a soft-deleted asset."""
        ctx = _make_context()
        created = create_asset(db, ctx, payload=asset_payload)

        ctx2 = _make_context()
        archive_asset(db, ctx2, asset_id=created.asset_id,
                      new_status="deleted")

        ctx3 = _make_context()
        restored = restore_asset(db, ctx3, asset_id=created.asset_id)
        assert restored.status.value == "active"
        assert restored.archived_at is None

    def test_restore_active_raises(self, db, project_id, asset_payload):
        """Restoring an already-active asset raises ValueError."""
        ctx = _make_context()
        created = create_asset(db, ctx, payload=asset_payload)

        ctx2 = _make_context()
        with pytest.raises(ValueError, match="Invalid status transition"):
            restore_asset(db, ctx2, asset_id=created.asset_id)

    def test_restore_missing_raises(self, db):
        """Restoring non-existent asset raises ValueError."""
        ctx = _make_context()
        with pytest.raises(ValueError, match="not found"):
            restore_asset(db, ctx, asset_id=uuid.uuid4())
