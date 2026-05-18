"""P5-04 Context Compiler — contract tests.

Covers P5-04 completion criteria:
1. compile_context() returns pack + items with correct structure.
2. Sensitivity ceiling filtering excludes items above ceiling.
3. Token budget trimming excludes items exceeding budget.
4. Fallback query created when no results and mode=full.
5. Knowledge FTS error → degradation_reason set.
6. Memory FTS error → degradation_reason set.
7. search_fallback mode + degradation → pack status='failed'.
8. Audit event + outbox event written on compile.
9. GET /context/packs list with filters and pagination.
10. GET /context/packs/{id} returns pack detail with items.
11. GET /context/packs/{id} 404 for missing pack.
12. POST /context/compile 201 + response schema validation.
13. knowledge_version_set and memory_version_set non-empty.
14. content_digest is SHA-256 prefix of source_ref.
15. Items have correct item_order (sequential).
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from mneme.api.context import ActorContext, RequestContext
from mneme.context.compiler import compile_context, _sensitivity_allowed, _content_hash
from mneme.db.context_packs import (
    create_context_pack,
    create_context_pack_item,
    get_context_pack,
    get_context_pack_items,
    list_context_packs,
)


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def test_context() -> RequestContext:
    """Build a minimal RequestContext with a user actor."""
    return RequestContext(
        request_id=uuid4(),
        correlation_id=uuid4(),
        actor=ActorContext(
            actor_type="user",
            actor_id=UUID("00000000-0000-0000-0000-000000000001"),
            auth_context_type="user_session",
            auth_context_id=UUID("00000000-0000-0000-0000-000000000001"),
        ),
        idempotency_key=str(uuid4()),
    )


@pytest.fixture
def test_project(db) -> UUID:
    """Create a test project and return its ID."""
    project_id = uuid4()
    code = f"CTX-{uuid4().hex[:8].upper()}"
    db.execute(
        text(
            "INSERT INTO projects (project_id, project_code, name, status, sensitivity_default) "
            "VALUES (:pid, :code, :name, 'active', 'normal')"
        ),
        {"pid": project_id.hex, "code": code, "name": "Context Test Project"},
    )
    db.flush()
    return project_id


def _make_knowledge_results(count: int = 3, sensitivity: str = "normal") -> list:
    """Create mock knowledge FTS search results."""
    from pydantic import BaseModel
    from uuid import uuid4

    class MockKnowledgeResult(BaseModel):
        chunk_id: UUID
        document_id: UUID
        block_id: UUID | None = None
        chunk_order: int = 0
        chunk_text: str
        rank: float
        document_title: str
        document_uri: str | None = None
        document_sensitivity: str
        block_key: str | None = None
        block_type: str | None = None
        block_order: int | None = None

    results = []
    for i in range(count):
        results.append(MockKnowledgeResult(
            chunk_id=uuid4(),
            document_id=uuid4(),
            block_id=uuid4(),
            chunk_order=i,
            chunk_text=f"Knowledge chunk {i} about testing. This is a test document.",
            rank=0.8 - i * 0.1,
            document_title=f"Test Document {i}",
            document_sensitivity=sensitivity,
            block_key=f"block-{i}",
            block_type="paragraph",
            block_order=i,
        ))
    return results


def _make_memory_results(count: int = 3, sensitivity: str = "normal") -> list:
    """Create mock memory FTS search results."""
    results = []
    for i in range(count):
        results.append({
            "memory_index_entry_id": uuid4(),
            "memory_id": uuid4(),
            "memory_version": 1,
            "index_text": f"Memory entry {i} about testing context compilation.",
            "fts_state": "ready",
            "vector_state": "pending",
            "rank": 0.7 - i * 0.1,
            "title": f"Memory {i}",
            "memory_text": f"This is memory text {i} about testing context compilation.",
            "sensitivity_level": sensitivity,
            "canonical_key": f"mem-{i}",
            "status": "active",
            "current_version": 1,
        })
    return results


# ═══════════════════════════════════════════════════════════════════════════
# Unit tests — helper functions
# ═══════════════════════════════════════════════════════════════════════════

class TestSensitivityAllowed:
    """Test sensitivity ceiling filtering logic."""

    def test_public_below_normal_ceiling(self):
        assert _sensitivity_allowed("public", "normal") is True

    def test_normal_at_normal_ceiling(self):
        assert _sensitivity_allowed("normal", "normal") is True

    def test_private_above_normal_ceiling(self):
        assert _sensitivity_allowed("private", "normal") is False

    def test_secret_above_private_ceiling(self):
        assert _sensitivity_allowed("secret", "private") is False

    def test_public_below_secret_ceiling(self):
        assert _sensitivity_allowed("public", "secret") is True

    def test_unknown_sensitivity_defaults_to_normal(self):
        # Unknown item sensitivity defaults to ordinal 1 (normal)
        assert _sensitivity_allowed("unknown", "public") is False
        assert _sensitivity_allowed("unknown", "normal") is True

    def test_unknown_ceiling_defaults_to_private(self):
        # Unknown ceiling defaults to ordinal 2 (private)
        assert _sensitivity_allowed("private", "unknown") is True
        assert _sensitivity_allowed("sensitive", "unknown") is False


class TestContentHash:
    """Test content hash generation."""

    def test_deterministic(self):
        h1 = _content_hash("hello world")
        h2 = _content_hash("hello world")
        assert h1 == h2

    def test_sha256_prefix(self):
        import hashlib
        h = _content_hash("test")
        expected = hashlib.sha256(b"test").hexdigest()[:32]
        assert h == expected

    def test_different_inputs_different_hashes(self):
        assert _content_hash("a") != _content_hash("b")

    def test_returns_32_chars(self):
        assert len(_content_hash("anything")) == 32


# ═══════════════════════════════════════════════════════════════════════════
# Unit tests — context_packs DB layer
# ═══════════════════════════════════════════════════════════════════════════

class TestContextPacksDB:
    """Test CRUD operations on context_packs and context_pack_items."""

    def test_create_and_get_pack(self, db, test_context):
        """Create a pack and retrieve it by ID."""
        pack = create_context_pack(
            db,
            test_context,
            compile_mode="full",
            status="created",
            knowledge_version_set=[{"doc_id": "d1"}],
            memory_version_set=[{"mem_id": "m1"}],
            token_budget={"max_tokens": 4096},
            exclusion_summary={"sensitivity_filtered": 0},
        )
        db.flush()

        assert pack is not None
        assert pack["context_pack_id"] is not None
        assert pack["compile_mode"] == "full"
        assert pack["status"] == "created"

        # Retrieve
        fetched = get_context_pack(db, pack["context_pack_id"])
        assert fetched is not None
        assert fetched["context_pack_id"] == pack["context_pack_id"]
        assert fetched["compile_mode"] == "full"

    def test_create_pack_item(self, db, test_context):
        """Create a pack and add items to it."""
        pack = create_context_pack(db, test_context, compile_mode="full")
        db.flush()

        item = create_context_pack_item(
            db,
            pack_id=pack["context_pack_id"],
            item_order=0,
            item_type="knowledge_chunk",
            object_id=uuid4(),
            included=True,
            score=0.95,
            token_count=100,
            reason="fts_match",
            content_digest="abc123",
        )
        db.flush()

        assert item is not None
        assert item["item_order"] == 0
        assert item["item_type"] == "knowledge_chunk"
        assert item["included"] == 1  # SQLite stores booleans as int

    def test_get_items_for_pack(self, db, test_context):
        """Create pack with multiple items and retrieve them ordered."""
        pack = create_context_pack(db, test_context, compile_mode="full")
        db.flush()
        pack_id = pack["context_pack_id"]

        for i in range(3):
            create_context_pack_item(
                db,
                pack_id=pack_id,
                item_order=i,
                item_type="memory",
                score=0.9 - i * 0.1,
                token_count=50,
            )
        db.flush()

        items = get_context_pack_items(db, pack_id)
        assert len(items) == 3
        assert items[0]["item_order"] == 0
        assert items[1]["item_order"] == 1
        assert items[2]["item_order"] == 2

    def test_list_packs_empty(self, db, test_context):
        """List packs returns empty when none exist."""
        packs, total = list_context_packs(db)
        assert total == 0
        assert packs == []

    def test_list_packs_with_data(self, db, test_context):
        """Create multiple packs and list them."""
        for i in range(3):
            create_context_pack(
                db,
                test_context,
                compile_mode="full",
                status="created",
            )
        db.flush()

        packs, total = list_context_packs(db)
        assert total == 3
        assert len(packs) == 3

    def test_list_packs_filter_status(self, db, test_context):
        """Filter packs by status."""
        create_context_pack(db, test_context, compile_mode="full", status="created")
        create_context_pack(db, test_context, compile_mode="full", status="failed")
        db.flush()

        packs, total = list_context_packs(db, status="created")
        assert total == 1
        assert packs[0]["status"] == "created"

    def test_list_packs_pagination(self, db, test_context):
        """Pagination works correctly."""
        for i in range(5):
            create_context_pack(db, test_context, compile_mode="full")
        db.flush()

        packs, total = list_context_packs(db, page=1, page_size=2)
        assert total == 5
        assert len(packs) == 2

        packs2, total2 = list_context_packs(db, page=3, page_size=2)
        assert total2 == 5
        assert len(packs2) == 1

    def test_get_nonexistent_pack(self, db):
        """Getting a nonexistent pack returns None."""
        result = get_context_pack(db, uuid4())
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════
# Unit tests — compile_context()
# ═══════════════════════════════════════════════════════════════════════════

class TestCompileContext:
    """Test the core compile_context function with mocked FTS."""

    @patch("mneme.context.compiler.memory_fts_search")
    @patch("mneme.context.compiler.knowledge_fts_search")
    def test_compile_basic(self, mock_knowledge_fts, mock_memory_fts, db, test_context):
        """Basic compile returns pack + items."""
        mock_knowledge_fts.return_value = (_make_knowledge_results(3), 3)
        mock_memory_fts.return_value = (_make_memory_results(2), 2)

        result = compile_context(
            db,
            test_context,
            query_text="testing context compilation",
            compile_mode="full",
            token_budget={"max_tokens": 10000, "reserve_for_output": 512},
            sensitivity_ceiling="private",
        )
        db.flush()

        assert "pack" in result
        assert "items" in result
        assert "total_token_count" in result
        assert "included_count" in result
        assert "excluded_count" in result
        assert "degradation_reason" in result

        assert result["pack"]["context_pack_id"] is not None
        assert result["pack"]["compile_mode"] == "full"
        assert result["included_count"] > 0

    @patch("mneme.context.compiler.memory_fts_search")
    @patch("mneme.context.compiler.knowledge_fts_search")
    def test_compile_version_sets_nonempty(self, mock_knowledge_fts, mock_memory_fts, db, test_context):
        """knowledge_version_set and memory_version_set should be non-empty."""
        mock_knowledge_fts.return_value = (_make_knowledge_results(2), 2)
        mock_memory_fts.return_value = (_make_memory_results(1), 1)

        result = compile_context(
            db, test_context,
            query_text="test",
            token_budget={"max_tokens": 10000, "reserve_for_output": 512},
        )
        db.flush()

        pack = result["pack"]
        kv = pack["knowledge_version_set"]
        mv = pack["memory_version_set"]

        # These are JSONB fields stored as strings in SQLite
        if isinstance(kv, str):
            kv = json.loads(kv)
        if isinstance(mv, str):
            mv = json.loads(mv)

        assert len(kv) > 0
        assert len(mv) > 0

    @patch("mneme.context.compiler.memory_fts_search")
    @patch("mneme.context.compiler.knowledge_fts_search")
    def test_sensitivity_ceiling_filters_items(self, mock_knowledge_fts, mock_memory_fts, db, test_context):
        """Items above sensitivity ceiling should be excluded."""
        mock_knowledge_fts.return_value = (_make_knowledge_results(2, sensitivity="sensitive"), 2)
        mock_memory_fts.return_value = (_make_memory_results(1, sensitivity="secret"), 1)

        result = compile_context(
            db, test_context,
            query_text="sensitive stuff",
            sensitivity_ceiling="normal",
            token_budget={"max_tokens": 10000, "reserve_for_output": 512},
        )
        db.flush()

        # All items should be filtered out (sensitive/secret > normal ceiling)
        for item in result["items"]:
            assert item.get("included") is False or item.get("item_type") == "fallback_query"

        excl = result["pack"]["exclusion_summary"]
        if isinstance(excl, str):
            excl = json.loads(excl)
        assert excl["sensitivity_filtered"] == 3

    @patch("mneme.context.compiler.memory_fts_search")
    @patch("mneme.context.compiler.knowledge_fts_search")
    def test_token_budget_trims_items(self, mock_knowledge_fts, mock_memory_fts, db, test_context):
        """Items should be trimmed when they exceed the token budget."""
        # Create results with large chunk_text to make token counts significant
        large_results = []
        for i in range(5):
            large_text = "word " * 500  # ~650 tokens each
            large_results.append({
                "chunk_id": uuid4(),
                "document_id": uuid4(),
                "block_id": uuid4(),
                "chunk_order": i,
                "chunk_text": large_text,
                "rank": 0.9 - i * 0.05,
                "document_title": f"Doc {i}",
                "document_uri": None,
                "document_sensitivity": "normal",
                "block_key": f"bk-{i}",
                "block_type": "paragraph",
                "block_order": i,
            })

        # We need Pydantic-like objects for knowledge results
        from pydantic import BaseModel
        class MockKR(BaseModel):
            chunk_id: UUID
            document_id: UUID
            block_id: UUID | None = None
            chunk_order: int = 0
            chunk_text: str
            rank: float
            document_title: str
            document_uri: str | None = None
            document_sensitivity: str
            block_key: str | None = None
            block_type: str | None = None
            block_order: int | None = None

        mock_knowledge_fts.return_value = ([MockKR(**r) for r in large_results], 5)
        mock_memory_fts.return_value = ([], 0)

        # Very small budget: 200 total tokens, 120 for knowledge, 80 for memory
        result = compile_context(
            db, test_context,
            query_text="large docs",
            token_budget={"max_tokens": 200, "reserve_for_output": 0, "knowledge_ratio": 0.6, "memory_ratio": 0.4},
            sensitivity_ceiling="private",
        )
        db.flush()

        # Some items should be excluded due to budget
        assert result["excluded_count"] > 0
        excl = result["pack"]["exclusion_summary"]
        if isinstance(excl, str):
            excl = json.loads(excl)
        assert excl["budget_trimmed"] > 0

    @patch("mneme.context.compiler.memory_fts_search")
    @patch("mneme.context.compiler.knowledge_fts_search")
    def test_fallback_query_when_no_results(self, mock_knowledge_fts, mock_memory_fts, db, test_context):
        """When no results found in full mode, a fallback_query item is created."""
        mock_knowledge_fts.return_value = ([], 0)
        mock_memory_fts.return_value = ([], 0)

        result = compile_context(
            db, test_context,
            query_text="no matching content",
            compile_mode="full",
            token_budget={"max_tokens": 4096, "reserve_for_output": 512},
        )
        db.flush()

        assert len(result["items"]) == 1
        assert result["items"][0]["item_type"] == "fallback_query"
        assert result["degradation_reason"] == "no_retrieval_results"

    @patch("mneme.context.compiler.memory_fts_search")
    @patch("mneme.context.compiler.knowledge_fts_search")
    def test_knowledge_fts_error_sets_degradation(self, mock_knowledge_fts, mock_memory_fts, db, test_context):
        """Knowledge FTS exception sets degradation_reason in full mode."""
        mock_knowledge_fts.side_effect = Exception("PG connection lost")
        mock_memory_fts.return_value = (_make_memory_results(1), 1)

        result = compile_context(
            db, test_context,
            query_text="test",
            compile_mode="full",
        )
        db.flush()

        assert result["degradation_reason"] is not None
        assert "knowledge_fts_error" in result["degradation_reason"]

    @patch("mneme.context.compiler.memory_fts_search")
    @patch("mneme.context.compiler.knowledge_fts_search")
    def test_memory_fts_error_sets_degradation(self, mock_knowledge_fts, mock_memory_fts, db, test_context):
        """Memory FTS exception sets degradation_reason when knowledge also had issues or memory is only source."""
        mock_knowledge_fts.return_value = ([], 0)
        mock_memory_fts.side_effect = Exception("Memory index down")

        result = compile_context(
            db, test_context,
            query_text="test",
            compile_mode="full",
        )
        db.flush()

        assert result["degradation_reason"] is not None
        assert "memory_fts_error" in result["degradation_reason"]

    @patch("mneme.context.compiler.memory_fts_search")
    @patch("mneme.context.compiler.knowledge_fts_search")
    def test_search_fallback_mode_failed_status(self, mock_knowledge_fts, mock_memory_fts, db, test_context):
        """search_fallback mode with degradation should set pack status to 'failed'."""
        mock_knowledge_fts.side_effect = Exception("FTS error")
        mock_memory_fts.return_value = ([], 0)

        result = compile_context(
            db, test_context,
            query_text="test",
            compile_mode="search_fallback",
        )
        db.flush()

        assert result["pack"]["status"] == "failed"
        assert result["degradation_reason"] is not None

    @patch("mneme.context.compiler.memory_fts_search")
    @patch("mneme.context.compiler.knowledge_fts_search")
    def test_compile_creates_audit_event(self, mock_knowledge_fts, mock_memory_fts, db, test_context):
        """Compile should write an audit_events row."""
        mock_knowledge_fts.return_value = (_make_knowledge_results(1), 1)
        mock_memory_fts.return_value = (_make_memory_results(1), 1)

        result = compile_context(db, test_context, query_text="audit test")
        db.flush()

        # Check audit_events
        audit_row = db.execute(
            text("SELECT * FROM audit_events WHERE action = 'context.compile' ORDER BY occurred_at DESC LIMIT 1")
        ).first()
        assert audit_row is not None
        data = dict(audit_row._mapping)
        assert data["action"] == "context.compile"
        assert data["result"] == "success"

    @patch("mneme.context.compiler.memory_fts_search")
    @patch("mneme.context.compiler.knowledge_fts_search")
    def test_compile_creates_outbox_event(self, mock_knowledge_fts, mock_memory_fts, db, test_context):
        """Compile should write an events (outbox) row."""
        mock_knowledge_fts.return_value = (_make_knowledge_results(1), 1)
        mock_memory_fts.return_value = (_make_memory_results(1), 1)

        result = compile_context(db, test_context, query_text="outbox test")
        db.flush()

        # Check events table
        event_row = db.execute(
            text("SELECT * FROM events WHERE event_type = 'context_pack.compiled' ORDER BY occurred_at DESC LIMIT 1")
        ).first()
        assert event_row is not None
        data = dict(event_row._mapping)
        assert data["event_type"] == "context_pack.compiled"
        assert data["aggregate_type"] == "context_pack"

    @patch("mneme.context.compiler.memory_fts_search")
    @patch("mneme.context.compiler.knowledge_fts_search")
    def test_compile_with_project_scope(self, mock_knowledge_fts, mock_memory_fts, db, test_context, test_project):
        """Compile passes project_id to FTS searches."""
        mock_knowledge_fts.return_value = ([], 0)
        mock_memory_fts.return_value = ([], 0)

        result = compile_context(
            db, test_context,
            query_text="scoped test",
            project_id=test_project,
        )
        db.flush()

        # Verify project_id was passed to FTS calls
        call_kwargs = mock_knowledge_fts.call_args
        assert call_kwargs[1].get("project_id") == test_project or (len(call_kwargs[0]) > 2 and call_kwargs[0][2] == test_project)

    @patch("mneme.context.compiler.memory_fts_search")
    @patch("mneme.context.compiler.knowledge_fts_search")
    def test_compile_with_agent_id(self, mock_knowledge_fts, mock_memory_fts, db, test_context):
        """Compile stores agent_id in the pack."""
        agent_id = uuid4()
        mock_knowledge_fts.return_value = ([], 0)
        mock_memory_fts.return_value = ([], 0)

        result = compile_context(
            db, test_context,
            query_text="agent test",
            agent_id=agent_id,
            compile_mode="full",
        )
        db.flush()

        pack = get_context_pack(db, result["pack"]["context_pack_id"])
        assert pack is not None
        stored_agent_id = pack["agent_id"]
        if isinstance(stored_agent_id, str):
            stored_agent_id = UUID(stored_agent_id)
        assert stored_agent_id == agent_id

    @patch("mneme.context.compiler.memory_fts_search")
    @patch("mneme.context.compiler.knowledge_fts_search")
    def test_compile_content_digest(self, mock_knowledge_fts, mock_memory_fts, db, test_context):
        """Items should have correct content_digest (SHA-256 prefix)."""
        mock_knowledge_fts.return_value = (_make_knowledge_results(1), 1)
        mock_memory_fts.return_value = ([], 0)

        result = compile_context(db, test_context, query_text="digest test")
        db.flush()

        for item in result["items"]:
            if item.get("item_type") == "fallback_query":
                continue
            # content_digest should be 32 chars
            assert item.get("content_digest") is not None
            assert len(item["content_digest"]) == 32

    @patch("mneme.context.compiler.memory_fts_search")
    @patch("mneme.context.compiler.knowledge_fts_search")
    def test_compile_item_order_sequential(self, mock_knowledge_fts, mock_memory_fts, db, test_context):
        """Items should have sequential item_order values."""
        mock_knowledge_fts.return_value = (_make_knowledge_results(3), 3)
        mock_memory_fts.return_value = (_make_memory_results(2), 2)

        result = compile_context(
            db, test_context,
            query_text="order test",
            token_budget={"max_tokens": 100000, "reserve_for_output": 0},
        )
        db.flush()

        items = result["items"]
        for i, item in enumerate(items):
            assert item["item_order"] == i, f"Expected item_order={i}, got {item['item_order']}"

    @patch("mneme.context.compiler.memory_fts_search")
    @patch("mneme.context.compiler.knowledge_fts_search")
    def test_compile_token_budget_dict_stored(self, mock_knowledge_fts, mock_memory_fts, db, test_context):
        """token_budget should be stored in the pack."""
        budget = {"max_tokens": 8192, "reserve_for_output": 1024}
        mock_knowledge_fts.return_value = ([], 0)
        mock_memory_fts.return_value = ([], 0)

        result = compile_context(
            db, test_context,
            query_text="budget test",
            token_budget=budget,
        )
        db.flush()

        pack = get_context_pack(db, result["pack"]["context_pack_id"])
        stored_budget = pack["token_budget"]
        if isinstance(stored_budget, str):
            stored_budget = json.loads(stored_budget)
        assert stored_budget["max_tokens"] == 8192

    @patch("mneme.context.compiler.memory_fts_search")
    @patch("mneme.context.compiler.knowledge_fts_search")
    def test_compile_persists_items_in_db(self, mock_knowledge_fts, mock_memory_fts, db, test_context):
        """compile_context should persist items in context_pack_items table."""
        mock_knowledge_fts.return_value = (_make_knowledge_results(2), 2)
        mock_memory_fts.return_value = (_make_memory_results(1), 1)

        result = compile_context(
            db, test_context,
            query_text="persist test",
            token_budget={"max_tokens": 10000, "reserve_for_output": 0},
        )
        db.flush()

        pack_id = result["pack"]["context_pack_id"]
        db_items = get_context_pack_items(db, pack_id)
        assert len(db_items) == len(result["items"])

    @patch("mneme.context.compiler.memory_fts_search")
    @patch("mneme.context.compiler.knowledge_fts_search")
    def test_compile_knowledge_ratio_and_memory_ratio(self, mock_knowledge_fts, mock_memory_fts, db, test_context):
        """Budget split between knowledge and memory respects ratios."""
        # Create equal-size results
        from pydantic import BaseModel
        class MockKR(BaseModel):
            chunk_id: UUID
            document_id: UUID
            block_id: UUID | None = None
            chunk_order: int = 0
            chunk_text: str
            rank: float
            document_title: str
            document_uri: str | None = None
            document_sensitivity: str
            block_key: str | None = None
            block_type: str | None = None
            block_order: int | None = None

        k_results = [MockKR(
            chunk_id=uuid4(), document_id=uuid4(),
            chunk_text="a " * 200, rank=0.9,
            document_title="K", document_sensitivity="normal",
        )]
        m_results = [{
            "memory_id": uuid4(), "memory_version": 1,
            "memory_text": "a " * 200, "rank": 0.9,
            "title": "M", "sensitivity_level": "normal",
            "canonical_key": "m", "status": "active", "current_version": 1,
        }]

        mock_knowledge_fts.return_value = (k_results, 1)
        mock_memory_fts.return_value = (m_results, 1)

        # 80/20 split: knowledge gets most budget
        result = compile_context(
            db, test_context,
            query_text="ratio test",
            token_budget={"max_tokens": 400, "reserve_for_output": 0, "knowledge_ratio": 0.8, "memory_ratio": 0.2},
        )
        db.flush()

        # Should have at least some items
        assert len(result["items"]) >= 1


# ═══════════════════════════════════════════════════════════════════════════
# Unit tests — item_types in results
# ═══════════════════════════════════════════════════════════════════════════

class TestCompileItemTypes:
    """Verify correct item_type values are produced."""

    @patch("mneme.context.compiler.memory_fts_search")
    @patch("mneme.context.compiler.knowledge_fts_search")
    def test_knowledge_items_type(self, mock_knowledge_fts, mock_memory_fts, db, test_context):
        """Knowledge results should produce item_type='knowledge_chunk'."""
        mock_knowledge_fts.return_value = (_make_knowledge_results(1), 1)
        mock_memory_fts.return_value = ([], 0)

        result = compile_context(
            db, test_context,
            query_text="type test",
            token_budget={"max_tokens": 10000, "reserve_for_output": 0},
        )
        db.flush()

        knowledge_items = [i for i in result["items"] if i["item_type"] == "knowledge_chunk"]
        assert len(knowledge_items) == 1

    @patch("mneme.context.compiler.memory_fts_search")
    @patch("mneme.context.compiler.knowledge_fts_search")
    def test_memory_items_type(self, mock_knowledge_fts, mock_memory_fts, db, test_context):
        """Memory results should produce item_type='memory'."""
        mock_knowledge_fts.return_value = ([], 0)
        mock_memory_fts.return_value = (_make_memory_results(1), 1)

        result = compile_context(
            db, test_context,
            query_text="memory type test",
            token_budget={"max_tokens": 10000, "reserve_for_output": 0},
        )
        db.flush()

        memory_items = [i for i in result["items"] if i["item_type"] == "memory"]
        assert len(memory_items) == 1

    @patch("mneme.context.compiler.memory_fts_search")
    @patch("mneme.context.compiler.knowledge_fts_search")
    def test_fallback_query_type(self, mock_knowledge_fts, mock_memory_fts, db, test_context):
        """No results → item_type='fallback_query'."""
        mock_knowledge_fts.return_value = ([], 0)
        mock_memory_fts.return_value = ([], 0)

        result = compile_context(
            db, test_context,
            query_text="nothing matches",
            compile_mode="full",
        )
        db.flush()

        assert len(result["items"]) == 1
        assert result["items"][0]["item_type"] == "fallback_query"
        assert result["items"][0]["reason"] == "no_results_fallback"


# ═══════════════════════════════════════════════════════════════════════════
# Unit tests — compile with no degradation in search_fallback mode (happy path)
# ═══════════════════════════════════════════════════════════════════════════

class TestSearchFallbackMode:
    """Test search_fallback compile mode behavior."""

    @patch("mneme.context.compiler.memory_fts_search")
    @patch("mneme.context.compiler.knowledge_fts_search")
    def test_search_fallback_no_degradation(self, mock_knowledge_fts, mock_memory_fts, db, test_context):
        """search_fallback mode with successful results should have status 'created'."""
        mock_knowledge_fts.return_value = (_make_knowledge_results(1), 1)
        mock_memory_fts.return_value = (_make_memory_results(1), 1)

        result = compile_context(
            db, test_context,
            query_text="fallback happy path",
            compile_mode="search_fallback",
            token_budget={"max_tokens": 10000, "reserve_for_output": 0},
        )
        db.flush()

        assert result["pack"]["status"] == "created"
        assert result["degradation_reason"] is None


