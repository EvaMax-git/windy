"""P3-08 Citation reference chains + source_maps — contract tests.

Covers
------
* build_citation(chunk_id) → full provenance chain
* list_citations(document_id) → all citations for a document
* check_stale_documents([document_ids]) → batch staleness
* is_document_stale(document_id) → individual staleness
* list_source_maps → debug enumeration
* GET /api/v4/knowledge/citations/{chunk_id}
* GET /api/v4/knowledge/documents/{id}/citations
* Search results with stale markers
"""

from __future__ import annotations

from typing import Generator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from mneme.db.base import SessionLocal
from mneme.knowledge.citation import (
    build_citation,
    check_stale_documents,
    is_document_stale,
    list_citations,
    list_source_maps,
    Citation,
    CitationListResult,
    CitationNode,
)

from mneme.schemas.knowledge import (
    KnowledgeDocumentCreate,
    KnowledgeBlockCreate,
    BlockType,
)
from mneme.schemas.common import SensitivityLevel
from mneme.api.context import RequestContext, ActorContext, with_actor
from mneme.db.knowledge import (
    create_document,
    add_block,
    get_document,
    list_blocks_by_document,
)


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    """Yield a database session; rollback after the test."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def test_context() -> RequestContext:
    """Minimal request context with idempotency key."""
    return RequestContext(
        request_id=str(uuid4()),
        correlation_id=str(uuid4()),
        idempotency_key=str(uuid4()),
        actor=ActorContext(
            actor_type="user",
            actor_id=UUID("00000000-0000-0000-0000-000000000001"),
            auth_context_type="user_session",
            auth_context_id=UUID("00000000-0000-0000-0000-000000000001"),
        ),
    )


@pytest.fixture
def test_project(db_session) -> UUID:
    """Create a project and return its ID."""
    project_id = str(uuid4())
    db_session.execute(
        text(
            "INSERT INTO projects (project_id, project_code, name, status, sensitivity_default) "
            "VALUES (:pid, :code, :name, 'active', 'normal')"
        ),
        {
            "pid": project_id,
            "code": f"CT-{uuid4().hex[:8].upper()}",
            "name": "P3-08 Citation Test Project",
        },
    )
    db_session.commit()
    return UUID(project_id)


# ── Helper: create a document with blocks and chunks ────────────────────


def _create_test_document(db_session, test_context, test_project, title="Test Doc",
                          with_asset=False, num_blocks=2):
    """Create a document with blocks and optionally an asset, return doc and blocks."""
    payload = KnowledgeDocumentCreate(
        project_id=test_project,
        title=title,
        source_asset_id=(uuid4() if with_asset else None),
    )
    if with_asset:
        # Create a minimal asset for the document to link to
        asset_id = payload.source_asset_id
        asset_uid = f"CT-{uuid4().hex[:12]}-{uuid4().hex[:8]}"
        db_session.execute(
            text(
                "INSERT INTO assets (asset_id, project_id, asset_uid, original_filename, title, "
                "asset_type, storage_ref, content_hash, ingest_state) "
                "VALUES (:aid, :pid, :uid, :fn, :title, 'document', 'pending', 'hash_ct', 'ready')"
            ),
            {
                "aid": str(asset_id),
                "pid": str(test_project),
                "uid": asset_uid,
                "fn": "test.txt",
                "title": "Citation Test Asset",
            },
        )
        db_session.commit()

    doc = create_document(db_session, test_context, payload=payload, project_code="CT")

    # Add blocks
    blocks = []
    for i in range(num_blocks):
        ctx = RequestContext(
            request_id=str(uuid4()), correlation_id=str(uuid4()),
            idempotency_key=str(uuid4()),
            actor=ActorContext(actor_type="user", actor_id=UUID("00000000-0000-0000-0000-000000000001")),
        )
        bp = KnowledgeBlockCreate(
            block_order=i,
            block_type=BlockType.paragraph,
            content_markdown=f"## Block {i}\n\nContent of block {i} for testing citations.",
        )
        block = add_block(db_session, ctx, document_id=doc.document_id, payload=bp)
        blocks.append(block)

    return doc, blocks


def _create_test_chunk(db_session, doc, block, chunk_order=0, chunk_text="Test chunk content"):
    """Insert a test chunk linked to a block."""
    chunk_id = str(uuid4())
    db_session.execute(
        text(
            "INSERT INTO knowledge_chunks (chunk_id, document_id, block_id, "
            "chunk_order, document_version, chunk_text, token_count) "
            "VALUES (:cid, :did, :bid, :co, :dv, :ct, :tk)"
        ),
        {
            "cid": chunk_id,
            "did": str(doc.document_id),
            "bid": str(block.block_id) if block else None,
            "co": chunk_order,
            "dv": doc.current_version,
            "ct": chunk_text,
            "tk": len(chunk_text.split()),
        },
    )
    # Also create a source_map for block→chunk citation
    db_session.execute(
        text(
            "INSERT INTO source_maps (source_map_id, project_id, source_type, source_id, "
            "target_type, target_id, source_block_id, target_document_id, target_chunk_id, "
            "span, mapping_role) "
            "VALUES (:smi, :pid, 'block', :sid, 'chunk', :tid, :sbi, :tdi, :tci, :sp, 'citation')"
        ),
        {
            "smi": str(uuid4()),
            "pid": str(doc.project_id),
            "sid": str(block.block_id),
            "tid": chunk_id,
            "sbi": str(block.block_id),
            "tdi": str(doc.document_id),
            "tci": chunk_id,
            "sp": '{"start": 0, "end": 20}',
        },
    )
    db_session.commit()
    return UUID(chunk_id)


def _ensure_index_state(db_session, document_id, fts_state="ready", citation_state="ready"):
    """Ensure an index_states row exists for a document."""
    db_session.execute(
        text(
            "INSERT INTO index_states (index_state_id, object_type, object_id, fts_state, citation_state) "
            "VALUES (:iid, 'knowledge_document', :oid, :fts, :cit) "
            "ON CONFLICT (object_type, object_id) DO UPDATE SET fts_state=:fts2, citation_state=:cit2"
        ),
        {
            "iid": str(uuid4()),
            "oid": str(document_id),
            "fts": fts_state,
            "cit": citation_state,
            "fts2": fts_state,
            "cit2": citation_state,
        },
    )
    db_session.commit()


# ═══════════════════════════════════════════════════════════════════════
# build_citation tests
# ═══════════════════════════════════════════════════════════════════════


class TestBuildCitation:
    """build_citation(chunk_id) — single chunk provenance chain."""

    def test_build_simple_citation(self, db_session, test_context, test_project):
        """Build a citation for a chunk with block and document (no asset)."""
        doc, blocks = _create_test_document(db_session, test_context, test_project)
        chunk_id = _create_test_chunk(db_session, doc, blocks[0], chunk_order=0,
                                       chunk_text="The answer is 42.")
        _ensure_index_state(db_session, doc.document_id)

        citation = build_citation(db_session, chunk_id=chunk_id)
        assert citation is not None
        assert citation.chunk_id == chunk_id
        assert citation.chunk_text == "The answer is 42."
        assert citation.chunk_order == 0
        assert citation.document_id == doc.document_id
        assert citation.document_title == "Test Doc"
        assert citation.document_version == doc.current_version
        assert len(citation.chain) >= 3  # chunk + block + document

        # Verify chain nodes
        types = [n.type for n in citation.chain]
        assert "chunk" in types
        assert "block" in types
        assert "document" in types

        # Document node should have title
        doc_node = next(n for n in citation.chain if n.type == "document")
        assert doc_node.label == "Test Doc"

    def test_build_citation_with_asset(self, db_session, test_context, test_project):
        """Build a citation for a chunk linked to an asset via source_maps."""
        doc, blocks = _create_test_document(db_session, test_context, test_project,
                                             with_asset=True)
        chunk_id = _create_test_chunk(db_session, doc, blocks[0])
        _ensure_index_state(db_session, doc.document_id)

        citation = build_citation(db_session, chunk_id=chunk_id)
        assert citation is not None
        assert len(citation.chain) >= 4  # chunk + block + document + asset

        types = [n.type for n in citation.chain]
        assert "asset" in types

        asset_node = next(n for n in citation.chain if n.type == "asset")
        assert asset_node.label is not None

    def test_build_nonexistent_chunk(self, db_session):
        """build_citation returns None for nonexistent chunk."""
        nonexistent = UUID("99999999-9999-9999-9999-999999999999")
        result = build_citation(db_session, chunk_id=nonexistent)
        assert result is None

    def test_citation_with_no_blocks(self, db_session, test_context, test_project):
        """Citation for chunk without a block should still return document info."""
        doc, _blocks = _create_test_document(db_session, test_context, test_project)
        # Create chunk without block_id
        chunk_id = str(uuid4())
        db_session.execute(
            text(
                "INSERT INTO knowledge_chunks (chunk_id, document_id, chunk_order, "
                "document_version, chunk_text, token_count) "
                "VALUES (:cid, :did, 0, 1, :ct, :tk)"
            ),
            {"cid": chunk_id, "did": str(doc.document_id), "ct": "Blockless chunk", "tk": 2},
        )
        db_session.commit()
        _ensure_index_state(db_session, doc.document_id)

        citation = build_citation(db_session, chunk_id=UUID(chunk_id))
        assert citation is not None
        # Should have chunk + document but no block
        types = [n.type for n in citation.chain]
        assert "chunk" in types
        assert "document" in types
        assert "block" not in types

    def test_citation_stale_when_fts_stale(self, db_session, test_context, test_project):
        """Citation should report is_stale=True when fts_state='stale'."""
        doc, blocks = _create_test_document(db_session, test_context, test_project)
        chunk_id = _create_test_chunk(db_session, doc, blocks[0])
        _ensure_index_state(db_session, doc.document_id, fts_state="stale", citation_state="ready")

        citation = build_citation(db_session, chunk_id=chunk_id)
        assert citation is not None
        assert citation.is_stale is True
        assert "fts_stale" in (citation.stale_reason or "")

    def test_citation_stale_when_citation_stale(self, db_session, test_context, test_project):
        """Citation should report is_stale=True when citation_state='stale'."""
        doc, blocks = _create_test_document(db_session, test_context, test_project)
        chunk_id = _create_test_chunk(db_session, doc, blocks[0])
        _ensure_index_state(db_session, doc.document_id, fts_state="ready", citation_state="stale")

        citation = build_citation(db_session, chunk_id=chunk_id)
        assert citation is not None
        assert citation.is_stale is True
        assert "citation_stale" in (citation.stale_reason or "")

    def test_citation_fresh(self, db_session, test_context, test_project):
        """Citation should report is_stale=False when all indexes are ready."""
        doc, blocks = _create_test_document(db_session, test_context, test_project)
        chunk_id = _create_test_chunk(db_session, doc, blocks[0])
        _ensure_index_state(db_session, doc.document_id, fts_state="ready", citation_state="ready")

        citation = build_citation(db_session, chunk_id=chunk_id)
        assert citation is not None
        assert citation.is_stale is False
        assert citation.stale_reason is None


# ═══════════════════════════════════════════════════════════════════════
# list_citations tests
# ═══════════════════════════════════════════════════════════════════════


class TestListCitations:
    """list_citations(document_id) — all citations for a document."""

    def test_list_citations_empty_document(self, db_session, test_context, test_project):
        """Listing citations for a document with no chunks returns empty list."""
        doc, _blocks = _create_test_document(db_session, test_context, test_project)
        result = list_citations(db_session, document_id=doc.document_id)
        assert isinstance(result, CitationListResult)
        assert result.document_id == doc.document_id
        assert result.total == 0
        assert result.citations == []

    def test_list_citations_multiple_chunks(self, db_session, test_context, test_project):
        """Multiple chunks for a document each get a citation."""
        doc, blocks = _create_test_document(db_session, test_context, test_project, num_blocks=2)
        _create_test_chunk(db_session, doc, blocks[0], chunk_order=0, chunk_text="Chunk A")
        _create_test_chunk(db_session, doc, blocks[1], chunk_order=1, chunk_text="Chunk B")
        _ensure_index_state(db_session, doc.document_id)

        result = list_citations(db_session, document_id=doc.document_id)
        assert result.total == 2
        assert len(result.citations) == 2
        assert result.citations[0].chunk_order == 0
        assert result.citations[1].chunk_order == 1

    def test_list_citations_chain_structure(self, db_session, test_context, test_project):
        """Each citation in the list has a complete chain."""
        doc, blocks = _create_test_document(db_session, test_context, test_project)
        _create_test_chunk(db_session, doc, blocks[0], chunk_order=0)
        _ensure_index_state(db_session, doc.document_id)

        result = list_citations(db_session, document_id=doc.document_id)
        assert result.total >= 1
        citation = result.citations[0]
        types = [n.type for n in citation.chain]
        assert "chunk" in types
        assert "document" in types

    def test_list_citations_with_stale_state(self, db_session, test_context, test_project):
        """All citations should share the document's stale state."""
        doc, blocks = _create_test_document(db_session, test_context, test_project)
        _create_test_chunk(db_session, doc, blocks[0], chunk_order=0)
        _ensure_index_state(db_session, doc.document_id, fts_state="stale")

        result = list_citations(db_session, document_id=doc.document_id)
        for citation in result.citations:
            assert citation.is_stale is True


# ═══════════════════════════════════════════════════════════════════════
# is_document_stale tests
# ═══════════════════════════════════════════════════════════════════════


class TestIsDocumentStale:
    """is_document_stale(document_id) — individual staleness check."""

    def test_fresh_document(self, db_session, test_context, test_project):
        """Document with fts_state='ready' and citation_state='ready' is fresh."""
        doc, _blocks = _create_test_document(db_session, test_context, test_project)
        _ensure_index_state(db_session, doc.document_id, fts_state="ready", citation_state="ready")

        is_stale, reason = is_document_stale(db_session, document_id=doc.document_id)
        assert is_stale is False
        assert reason is None

    def test_stale_fts(self, db_session, test_context, test_project):
        """Document with fts_state='stale' is stale."""
        doc, _blocks = _create_test_document(db_session, test_context, test_project)
        _ensure_index_state(db_session, doc.document_id, fts_state="stale", citation_state="ready")

        is_stale, reason = is_document_stale(db_session, document_id=doc.document_id)
        assert is_stale is True
        assert reason == "fts_stale"

    def test_stale_citation(self, db_session, test_context, test_project):
        """Document with citation_state='stale' is stale."""
        doc, _blocks = _create_test_document(db_session, test_context, test_project)
        _ensure_index_state(db_session, doc.document_id, fts_state="ready", citation_state="stale")

        is_stale, reason = is_document_stale(db_session, document_id=doc.document_id)
        assert is_stale is True
        assert reason == "citation_stale"

    def test_no_index_state(self, db_session, test_context, test_project):
        """Document with no index_states row is stale (no_index_state)."""
        doc, _blocks = _create_test_document(db_session, test_context, test_project)

        is_stale, reason = is_document_stale(db_session, document_id=doc.document_id)
        assert is_stale is True
        assert reason == "no_index_state"

    def test_reason_pending(self, db_session, test_context, test_project):
        """Document with fts_state='pending' is stale with reason fts_pending."""
        doc, _blocks = _create_test_document(db_session, test_context, test_project)
        _ensure_index_state(db_session, doc.document_id, fts_state="pending", citation_state="pending")

        is_stale, reason = is_document_stale(db_session, document_id=doc.document_id)
        assert is_stale is True
        assert "pending" in (reason or "")


# ═══════════════════════════════════════════════════════════════════════
# check_stale_documents tests
# ═══════════════════════════════════════════════════════════════════════


class TestCheckStaleDocuments:
    """check_stale_documents — batch staleness check."""

    def test_empty_list(self, db_session):
        """Empty document list returns empty dict."""
        result = check_stale_documents(db_session, document_ids=[])
        assert result == {}

    def test_mixed_states(self, db_session, test_context, test_project):
        """Documents with different states are correctly reported."""
        doc1, _ = _create_test_document(db_session, test_context, test_project, title="Doc1")
        doc2, _ = _create_test_document(db_session, test_context, test_project, title="Doc2")
        doc3, _ = _create_test_document(db_session, test_context, test_project, title="Doc3")

        _ensure_index_state(db_session, doc1.document_id, fts_state="ready", citation_state="ready")
        _ensure_index_state(db_session, doc2.document_id, fts_state="stale", citation_state="ready")
        # doc3 has no index_state

        result = check_stale_documents(db_session, document_ids=[
            doc1.document_id, doc2.document_id, doc3.document_id,
        ])
        # doc1: ready,ready
        assert result.get(doc1.document_id) == ("ready", "ready")
        # doc2: stale,ready
        assert result.get(doc2.document_id) == ("stale", "ready")
        # doc3: no index_state → absent from result
        assert doc3.document_id not in result

    def test_single_document(self, db_session, test_context, test_project):
        """Single document check works."""
        doc, _ = _create_test_document(db_session, test_context, test_project)
        _ensure_index_state(db_session, doc.document_id, fts_state="ready")

        result = check_stale_documents(db_session, document_ids=[doc.document_id])
        assert len(result) == 1
        fts, _ = result[doc.document_id]
        assert fts == "ready"


# ═══════════════════════════════════════════════════════════════════════
# list_source_maps tests
# ═══════════════════════════════════════════════════════════════════════


class TestListSourceMaps:
    """list_source_maps — debug enumeration of source_maps rows."""

    def test_list_source_maps_by_document(self, db_session, test_context, test_project):
        """source_maps for a document can be listed."""
        doc, blocks = _create_test_document(db_session, test_context, test_project,
                                             with_asset=True)
        chunk_id = _create_test_chunk(db_session, doc, blocks[0])

        maps = list_source_maps(db_session, document_id=doc.document_id)
        assert len(maps) >= 1  # at least the block→chunk citation source_map
        # Check that chunk source_map exists
        chunk_maps = [m for m in maps if m.get("target_chunk_id") == chunk_id]
        assert len(chunk_maps) >= 1

    def test_list_source_maps_empty(self, db_session, test_context, test_project):
        """Document with no source_maps returns empty list."""
        doc, _blocks = _create_test_document(db_session, test_context, test_project,
                                              with_asset=False)
        maps = list_source_maps(db_session, document_id=doc.document_id)
        # Should only have auto-created derived_from maps (or none if no asset)
        for m in maps:
            assert m.get("target_document_id") == doc.document_id or \
                   m.get("source_document_id") == doc.document_id


# ═══════════════════════════════════════════════════════════════════════
# API endpoint tests (using FastAPI TestClient)
# ═══════════════════════════════════════════════════════════════════════


class TestCitationAPI:
    """API endpoints for citation (GET /knowledge/citations/..., /documents/.../citations)."""

    @pytest.fixture
    def app(self):
        """Create FastAPI test app."""
        from mneme.main import create_app
        return create_app()

    @pytest.fixture
    def client(self, app):
        """FastAPI TestClient."""
        from fastapi.testclient import TestClient
        return TestClient(app)

    def test_get_chunk_citation_404(self, client):
        """GET /knowledge/citations/{chunk_id} returns 404 for nonexistent chunk."""
        resp = client.get(
            "/api/v4/knowledge/citations/99999999-9999-9999-9999-999999999999"
        )
        assert resp.status_code == 404

    def test_get_document_citations_404(self, client):
        """GET /knowledge/documents/{id}/citations returns 404 for nonexistent doc."""
        resp = client.get(
            "/api/v4/knowledge/documents/99999999-9999-9999-9999-999999999999/citations"
        )
        assert resp.status_code == 404

    def test_citation_chain_data_types(self, db_session, test_context, test_project):
        """Citation data structures contain valid UUID objects."""
        doc, blocks = _create_test_document(db_session, test_context, test_project)
        chunk_id = _create_test_chunk(db_session, doc, blocks[0])
        _ensure_index_state(db_session, doc.document_id)

        citation = build_citation(db_session, chunk_id=chunk_id)
        assert isinstance(citation.chunk_id, UUID)
        for node in citation.chain:
            assert isinstance(node.id, UUID)
            assert isinstance(node.type, str)
            assert node.type in ("chunk", "block", "document", "asset")

    def test_citation_serializable(self, db_session, test_context, test_project):
        """Citation objects are JSON-serializable via fastapi.encoders."""
        from fastapi.encoders import jsonable_encoder

        doc, blocks = _create_test_document(db_session, test_context, test_project)
        chunk_id = _create_test_chunk(db_session, doc, blocks[0])
        _ensure_index_state(db_session, doc.document_id)

        citation = build_citation(db_session, chunk_id=chunk_id)
        # Should not raise
        data = jsonable_encoder(citation)
        assert "chunk_id" in data
        assert "chain" in data
        assert isinstance(data["chain"], list)
        assert len(data["chain"]) >= 3


# ═══════════════════════════════════════════════════════════════════════
# Stale marker in search context (P3-08 integration)
# ═══════════════════════════════════════════════════════════════════════


class TestSearchStaleMarkers:
    """Stale markers integrated with search results."""

    def test_stale_marker_on_search_result(self, db_session, test_context, test_project):
        """Search results should have is_stale=True when document index is stale."""
        from fastapi.encoders import jsonable_encoder

        doc, blocks = _create_test_document(db_session, test_context, test_project)
        _create_test_chunk(db_session, doc, blocks[0], chunk_order=0,
                           chunk_text="unique_search_term_xyz")
        _ensure_index_state(db_session, doc.document_id, fts_state="stale")

        # Simulate what the search endpoint does: check stale documents
        stale_states = check_stale_documents(db_session, document_ids=[doc.document_id])
        state = stale_states.get(doc.document_id)
        assert state is not None
        fts_state, _ = state
        assert fts_state == "stale"

    def test_fresh_document_not_stale_in_search(self, db_session, test_context, test_project):
        """Search results should have is_stale=False when document index is ready."""
        doc, blocks = _create_test_document(db_session, test_context, test_project)
        _create_test_chunk(db_session, doc, blocks[0])
        _ensure_index_state(db_session, doc.document_id, fts_state="ready")

        stale_states = check_stale_documents(db_session, document_ids=[doc.document_id])
        state = stale_states.get(doc.document_id)
        assert state is not None
        fts_state, _ = state
        assert fts_state == "ready"
