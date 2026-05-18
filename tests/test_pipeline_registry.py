"""Contract tests for Pipeline Registry DAL.

Tests the ``mneme.db.pipeline_registry`` module covering:

* CRUD: create / list / get / get_by_module / delete
* Seed: seed_pipeline_registries inserts 3 default pipelines
* Idempotency: seed is idempotent
"""

from __future__ import annotations

import uuid

import pytest

from mneme.db.pipeline_registry import (
    create_pipeline_registry,
    delete_pipeline_registry,
    get_pipeline_registry,
    get_pipeline_registry_by_module,
    list_pipeline_registries,
    seed_pipeline_registries,
)
from mneme.schemas.pipeline_registry import (
    PipelineRegistryCreateRequest,
)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _make_payload(**overrides) -> PipelineRegistryCreateRequest:
    """Build a PipelineRegistryCreateRequest with defaults."""
    defaults = {
        "name": "Test Pipeline",
        "input_formats": ["text/plain", "text/markdown"],
        "processor_module": f"test_module_{uuid.uuid4().hex[:8]}",
        "accept_chunk_types": ["text", "code"],
        "target_stores": ["knowledge_store"],
    }
    defaults.update(overrides)
    return PipelineRegistryCreateRequest.model_validate(defaults)


# ═══════════════════════════════════════════════════════════════════════════════
# Pipeline Registry CRUD
# ═══════════════════════════════════════════════════════════════════════════════


class TestPipelineRegistryCRUD:

    def test_create_basic(self, db):
        payload = _make_payload(name="Standard Chunking")
        result = create_pipeline_registry(db, payload)

        assert result.id is not None
        assert result.name == "Standard Chunking"
        assert result.processor_module.startswith("test_module_")
        assert "text/plain" in result.input_formats
        assert "text" in result.accept_chunk_types
        assert "knowledge_store" in result.target_stores
        assert result.created_at is not None

    def test_list_all(self, db):
        create_pipeline_registry(db, _make_payload())
        create_pipeline_registry(db, _make_payload())
        create_pipeline_registry(db, _make_payload())

        items = list_pipeline_registries(db)
        assert len(items) >= 3

        # Verify ordering by created_at ASC
        for i in range(1, len(items)):
            assert items[i - 1].created_at <= items[i].created_at

    def test_get_by_id(self, db):
        created = create_pipeline_registry(db, _make_payload())
        fetched = get_pipeline_registry(db, created.id)
        assert fetched is not None
        assert fetched.id == created.id
        assert fetched.name == created.name

    def test_get_by_id_not_found(self, db):
        assert get_pipeline_registry(db, uuid.uuid4()) is None

    def test_get_by_module(self, db):
        payload = _make_payload(processor_module="unique_ocr_module")
        created = create_pipeline_registry(db, payload)
        fetched = get_pipeline_registry_by_module(db, "unique_ocr_module")
        assert fetched is not None
        assert fetched.id == created.id
        assert fetched.processor_module == "unique_ocr_module"

    def test_get_by_module_not_found(self, db):
        assert get_pipeline_registry_by_module(db, "nonexistent_module") is None

    def test_delete_existing(self, db):
        created = create_pipeline_registry(db, _make_payload())
        deleted = delete_pipeline_registry(db, created.id)
        assert deleted is not None
        assert deleted.id == created.id

        # Verify it's actually gone
        assert get_pipeline_registry(db, created.id) is None

    def test_delete_nonexistent(self, db):
        result = delete_pipeline_registry(db, uuid.uuid4())
        assert result is None

    def test_create_with_all_chunk_types(self, db):
        payload = _make_payload(
            accept_chunk_types=["text", "code", "table", "image", "conversation"],
        )
        result = create_pipeline_registry(db, payload)
        assert len(result.accept_chunk_types) == 5
        assert "conversation" in result.accept_chunk_types

    def test_create_with_multiple_target_stores(self, db):
        payload = _make_payload(
            target_stores=["knowledge_store", "memory_store", "vector_store"],
        )
        result = create_pipeline_registry(db, payload)
        assert len(result.target_stores) == 3

    def test_create_with_image_input_formats(self, db):
        payload = _make_payload(
            input_formats=["image/png", "image/jpeg", "image/tiff"],
        )
        result = create_pipeline_registry(db, payload)
        assert result.input_formats == ["image/png", "image/jpeg", "image/tiff"]


# ═══════════════════════════════════════════════════════════════════════════════
# Seed data
# ═══════════════════════════════════════════════════════════════════════════════


class TestSeedPipelines:

    def test_seed_inserts_three_pipelines(self, db):
        # Ensure empty state first
        existing = list_pipeline_registries(db)
        for item in existing:
            delete_pipeline_registry(db, item.id)

        created = seed_pipeline_registries(db)
        assert len(created) == 3

        # Verify all three processor modules are present
        modules = {r.processor_module for r in created}
        assert modules == {"standard_chunking", "ocr", "conversation_parser"}

    def test_seed_is_idempotent(self, db):
        # First seed
        seed_pipeline_registries(db)
        first_count = len(list_pipeline_registries(db))

        # Second seed should not create duplicates
        created = seed_pipeline_registries(db)
        assert len(created) == 0
        assert len(list_pipeline_registries(db)) == first_count

    def test_seed_preserves_existing(self, db):
        # Add a custom pipeline first
        create_pipeline_registry(db, _make_payload(processor_module="custom_pipeline"))

        # Seed should not overwrite or delete it
        seed_pipeline_registries(db)
        all_items = list_pipeline_registries(db)

        modules = {r.processor_module for r in all_items}
        assert "custom_pipeline" in modules
        assert "standard_chunking" in modules
        assert "ocr" in modules
        assert "conversation_parser" in modules

    def test_seed_standard_chunking_has_correct_formats(self, db):
        seed_pipeline_registries(db)
        item = get_pipeline_registry_by_module(db, "standard_chunking")
        assert item is not None
        assert item.name == "标准分块"
        assert "text/plain" in item.input_formats
        assert "application/pdf" in item.input_formats
        assert "text" in item.accept_chunk_types
        assert "code" in item.accept_chunk_types
        assert "table" in item.accept_chunk_types
        assert item.target_stores == ["knowledge_store"]

    def test_seed_ocr_has_correct_formats(self, db):
        seed_pipeline_registries(db)
        item = get_pipeline_registry_by_module(db, "ocr")
        assert item is not None
        assert item.name == "OCR"
        assert "image/png" in item.input_formats
        assert "image/jpeg" in item.input_formats
        assert "image" in item.accept_chunk_types
        assert item.target_stores == ["knowledge_store"]

    def test_seed_conversation_parser_has_correct_formats(self, db):
        seed_pipeline_registries(db)
        item = get_pipeline_registry_by_module(db, "conversation_parser")
        assert item is not None
        assert item.name == "对话解析"
        assert "application/json" in item.input_formats
        assert "text/csv" in item.input_formats
        assert "conversation" in item.accept_chunk_types
        assert "message" in item.accept_chunk_types
        assert item.target_stores == ["memory_store"]
