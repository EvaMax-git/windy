"""P4-04 Memory Candidates — contract tests (DB layer).

Covers P4-04 completion criteria:
1. Submit candidate → candidate_hash auto-computed, dedup via UNIQUE(project_id, candidate_hash).
2. Get candidate by ID (found / not-found).
3. List candidates with filters (project_id, source_type, candidate_status) and pagination.
4. Update candidate mutable fields.
5. Status transitions: pending_review → approved, pending_review → rejected.
6. Invalid status transitions rejected.
7. Delete candidate (hard delete).
8. Idempotency: duplicate submission with same hash returns existing.
9. Edge cases: all enum values, unicode, large text, confidence_score boundaries.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from mneme.api.context import ActorContext, RequestContext
from mneme.db.memory_candidates import (
    compute_candidate_hash,
    delete_candidate,
    get_candidate_by_hash,
    get_candidate_by_id,
    list_candidates,
    submit_candidate,
    update_candidate,
    update_candidate_status,
)
from mneme.schemas.memory_candidates import (
    CandidateSourceType,
    CandidateStatus,
    MemoryCandidateCreate,
    MemoryCandidateUpdate,
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
    """Create a project and return its ID."""
    project_id = uuid4()
    code = f"MC-{uuid4().hex[:8].upper()}"
    db.execute(
        text(
            "INSERT INTO projects (project_id, project_code, name, status, sensitivity_default) "
            "VALUES (:pid, :code, :name, 'active', 'normal')"
        ),
        {"pid": project_id.hex, "code": code, "name": "P4-04 Test Project"},
    )
    db.flush()
    return project_id


@pytest.fixture
def test_project_2(db) -> UUID:
    """Create a second project for cross-project isolation tests."""
    project_id = uuid4()
    code = f"MC2-{uuid4().hex[:8].upper()}"
    db.execute(
        text(
            "INSERT INTO projects (project_id, project_code, name, status, sensitivity_default) "
            "VALUES (:pid, :code, :name, 'active', 'normal')"
        ),
        {"pid": project_id.hex, "code": code, "name": "P4-04 Second Test Project"},
    )
    db.flush()
    return project_id


@pytest.fixture(autouse=True)
def ensure_memory_candidates_table(db):
    """Ensure the memory_candidates table exists for SQLite."""
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS memory_candidates (
            candidate_id TEXT PRIMARY KEY NOT NULL,
            project_id TEXT,
            source_type TEXT NOT NULL,
            source_id TEXT,
            submitted_by_actor_type TEXT NOT NULL,
            submitted_by_actor_id TEXT,
            title TEXT,
            candidate_text TEXT NOT NULL,
            candidate_hash TEXT NOT NULL,
            sensitivity_level TEXT NOT NULL DEFAULT 'private',
            candidate_status TEXT NOT NULL DEFAULT 'pending_review',
            confidence_score REAL,
            review_required INTEGER NOT NULL DEFAULT 1,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (project_id, candidate_hash)
        )
    """))
    db.flush()
    yield


def _make_context(test_context: RequestContext) -> RequestContext:
    """Return a fresh context with a unique idempotency key."""
    return RequestContext(
        request_id=test_context.request_id,
        correlation_id=test_context.correlation_id,
        actor=test_context.actor,
        idempotency_key=str(uuid4()),
    )


# ═══════════════════════════════════════════════════════════════════════════
# Unit Tests — compute_candidate_hash
# ═══════════════════════════════════════════════════════════════════════════


class TestCandidateHash:
    """Unit tests for the compute_candidate_hash helper."""

    def test_deterministic(self):
        """Same inputs produce same hash."""
        h1 = compute_candidate_hash(
            title="Test",
            candidate_text="Hello world",
            source_type="manual",
            source_id=None,
        )
        h2 = compute_candidate_hash(
            title="Test",
            candidate_text="Hello world",
            source_type="manual",
            source_id=None,
        )
        assert h1 == h2
        assert len(h1) == 64  # SHA-256 hex digest

    def test_different_text_produces_different_hash(self):
        """Different candidate_text produces different hash."""
        h1 = compute_candidate_hash(
            title="T",
            candidate_text="Alpha",
            source_type="manual",
            source_id=None,
        )
        h2 = compute_candidate_hash(
            title="T",
            candidate_text="Beta",
            source_type="manual",
            source_id=None,
        )
        assert h1 != h2

    def test_different_title_produces_different_hash(self):
        """Different title produces different hash."""
        h1 = compute_candidate_hash(
            title="Title A",
            candidate_text="Same text",
            source_type="manual",
            source_id=None,
        )
        h2 = compute_candidate_hash(
            title="Title B",
            candidate_text="Same text",
            source_type="manual",
            source_id=None,
        )
        assert h1 != h2

    def test_different_source_type_produces_different_hash(self):
        """Different source_type produces different hash."""
        h1 = compute_candidate_hash(
            title="T",
            candidate_text="Same text",
            source_type="manual",
            source_id=None,
        )
        h2 = compute_candidate_hash(
            title="T",
            candidate_text="Same text",
            source_type="message",
            source_id=None,
        )
        assert h1 != h2

    def test_different_source_id_produces_different_hash(self):
        """Different source_id produces different hash."""
        sid1 = uuid4()
        sid2 = uuid4()
        h1 = compute_candidate_hash(
            title="T",
            candidate_text="Same",
            source_type="message",
            source_id=sid1,
        )
        h2 = compute_candidate_hash(
            title="T",
            candidate_text="Same",
            source_type="message",
            source_id=sid2,
        )
        assert h1 != h2

    def test_none_title_included_as_empty(self):
        """None title is treated as empty string in hash."""
        h1 = compute_candidate_hash(
            title=None,
            candidate_text="Text",
            source_type="manual",
            source_id=None,
        )
        h2 = compute_candidate_hash(
            title="",
            candidate_text="Text",
            source_type="manual",
            source_id=None,
        )
        assert h1 == h2

    def test_none_source_id_included_as_empty(self):
        """None source_id is treated as empty string in hash."""
        h1 = compute_candidate_hash(
            title="T",
            candidate_text="Text",
            source_type="manual",
            source_id=None,
        )
        h2 = compute_candidate_hash(
            title="T",
            candidate_text="Text",
            source_type="manual",
            source_id=UUID("00000000-0000-0000-0000-000000000000"),
        )
        assert h1 != h2  # None vs explicit UUID produce different hashes

    def test_unicode_in_hash_input(self):
        """Unicode characters are handled correctly in hash computation."""
        h = compute_candidate_hash(
            title="记忆",
            candidate_text="你好世界 🌍",
            source_type="manual",
            source_id=None,
        )
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)


# ═══════════════════════════════════════════════════════════════════════════
# 1. Submit Candidate
# ═══════════════════════════════════════════════════════════════════════════


class TestSubmitCandidate:
    """POST /api/v4/memory/candidates — submit a new memory candidate."""

    def test_submit_minimal(self, db, test_context, test_project):
        """Submit a candidate with only required fields succeeds."""
        ctx = _make_context(test_context)
        payload = MemoryCandidateCreate(
            project_id=test_project,
            source_type=CandidateSourceType.manual,
            candidate_text="This is a test memory candidate.",
        )
        candidate = submit_candidate(db, ctx, payload=payload)

        assert candidate.candidate_id is not None
        assert candidate.project_id == test_project
        assert candidate.source_type == "manual"
        assert candidate.candidate_text == "This is a test memory candidate."
        assert candidate.candidate_hash is not None
        assert len(candidate.candidate_hash) == 64
        assert candidate.submitted_by_actor_type == "user"
        assert candidate.submitted_by_actor_id == UUID("00000000-0000-0000-0000-000000000001")
        assert candidate.sensitivity_level == "private"
        assert candidate.candidate_status == "pending_review"
        assert candidate.confidence_score is None
        assert candidate.review_required is True
        assert candidate.metadata_json == {}
        assert candidate.created_at is not None

    def test_submit_auto_computes_candidate_hash(self, db, test_context, test_project):
        """candidate_hash is auto-computed as SHA-256 of key fields."""
        ctx = _make_context(test_context)
        source_id = uuid4()
        payload = MemoryCandidateCreate(
            project_id=test_project,
            source_type=CandidateSourceType.message,
            source_id=source_id,
            title="My Memory",
            candidate_text="Important information from conversation.",
        )
        candidate = submit_candidate(db, ctx, payload=payload)

        expected_hash = compute_candidate_hash(
            title="My Memory",
            candidate_text="Important information from conversation.",
            source_type="message",
            source_id=source_id,
        )
        assert candidate.candidate_hash == expected_hash

    def test_submit_with_all_optional_fields(self, db, test_context, test_project):
        """Submit a candidate with all optional fields populated."""
        ctx = _make_context(test_context)
        source_id = uuid4()
        payload = MemoryCandidateCreate(
            project_id=test_project,
            source_type=CandidateSourceType.agent_submission,
            source_id=source_id,
            title="Agent-generated memory",
            candidate_text="The user prefers dark mode in all applications.",
            sensitivity_level="sensitive",
            confidence_score=0.85,
            review_required=True,
            metadata_json={"agent": "preference_extractor", "run_id": "run-001"},
        )
        candidate = submit_candidate(db, ctx, payload=payload)

        assert candidate.title == "Agent-generated memory"
        assert candidate.source_type == "agent_submission"
        assert candidate.source_id == source_id
        assert candidate.sensitivity_level == "sensitive"
        assert candidate.confidence_score == 0.85
        assert candidate.review_required is True
        assert candidate.metadata_json == {"agent": "preference_extractor", "run_id": "run-001"}

    def test_submit_without_project_id(self, db, test_context):
        """Submitting a candidate without project_id is allowed (project_id is nullable)."""
        ctx = _make_context(test_context)
        payload = MemoryCandidateCreate(
            source_type=CandidateSourceType.manual,
            candidate_text="Global candidate without project.",
        )
        candidate = submit_candidate(db, ctx, payload=payload)

        assert candidate.candidate_id is not None
        assert candidate.project_id is None
        assert candidate.candidate_status == "pending_review"

    def test_submit_with_confidence_score_zero(self, db, test_context, test_project):
        """Confidence score of exactly 0.0 is accepted."""
        ctx = _make_context(test_context)
        payload = MemoryCandidateCreate(
            project_id=test_project,
            source_type=CandidateSourceType.agent_submission,
            candidate_text="Low confidence candidate.",
            confidence_score=0.0,
        )
        candidate = submit_candidate(db, ctx, payload=payload)
        assert candidate.confidence_score == 0.0

    def test_submit_with_confidence_score_one(self, db, test_context, test_project):
        """Confidence score of exactly 1.0 is accepted."""
        ctx = _make_context(test_context)
        payload = MemoryCandidateCreate(
            project_id=test_project,
            source_type=CandidateSourceType.agent_submission,
            candidate_text="High confidence candidate.",
            confidence_score=1.0,
        )
        candidate = submit_candidate(db, ctx, payload=payload)
        assert candidate.confidence_score == 1.0

    def test_submit_review_required_defaults_true(self, db, test_context, test_project):
        """review_required defaults to True."""
        ctx = _make_context(test_context)
        payload = MemoryCandidateCreate(
            project_id=test_project,
            source_type=CandidateSourceType.manual,
            candidate_text="Default review required.",
        )
        candidate = submit_candidate(db, ctx, payload=payload)
        assert candidate.review_required is True

    def test_submit_review_required_false(self, db, test_context, test_project):
        """review_required can be explicitly set to False."""
        ctx = _make_context(test_context)
        payload = MemoryCandidateCreate(
            project_id=test_project,
            source_type=CandidateSourceType.agent_submission,
            candidate_text="Auto-approve candidate.",
            review_required=False,
        )
        candidate = submit_candidate(db, ctx, payload=payload)
        assert candidate.review_required is False

    def test_submit_all_source_types(self, db, test_context, test_project):
        """All CandidateSourceType enum values are accepted."""
        for source_type in CandidateSourceType:
            ctx = _make_context(test_context)
            payload = MemoryCandidateCreate(
                project_id=test_project,
                source_type=source_type,
                candidate_text=f"Candidate from {source_type.value}.",
            )
            candidate = submit_candidate(db, ctx, payload=payload)
            assert candidate.source_type == source_type.value


# ═══════════════════════════════════════════════════════════════════════════
# 2. Get Candidate
# ═══════════════════════════════════════════════════════════════════════════


class TestGetCandidate:
    """GET /api/v4/memory/candidates/{id} — get candidate by ID."""

    def test_get_existing(self, db, test_context, test_project):
        """Get a candidate that exists returns it."""
        ctx = _make_context(test_context)
        payload = MemoryCandidateCreate(
            project_id=test_project,
            source_type=CandidateSourceType.manual,
            candidate_text="Candidate to retrieve.",
            title="Retrieval Test",
        )
        created = submit_candidate(db, ctx, payload=payload)

        fetched = get_candidate_by_id(db, created.candidate_id)
        assert fetched is not None
        assert fetched.candidate_id == created.candidate_id
        assert fetched.title == "Retrieval Test"
        assert fetched.candidate_text == "Candidate to retrieve."
        assert fetched.candidate_hash == created.candidate_hash

    def test_get_nonexistent(self, db):
        """Get a non-existent candidate returns None."""
        result = get_candidate_by_id(db, UUID("99999999-9999-9999-9999-999999999999"))
        assert result is None

    def test_get_by_hash_found(self, db, test_context, test_project):
        """Get candidate by project_id + candidate_hash returns it."""
        ctx = _make_context(test_context)
        payload = MemoryCandidateCreate(
            project_id=test_project,
            source_type=CandidateSourceType.manual,
            candidate_text="Hash lookup test.",
        )
        created = submit_candidate(db, ctx, payload=payload)

        fetched = get_candidate_by_hash(
            db,
            project_id=test_project,
            candidate_hash=created.candidate_hash,
        )
        assert fetched is not None
        assert fetched.candidate_id == created.candidate_id

    def test_get_by_hash_not_found_wrong_project(self, db, test_context, test_project, test_project_2):
        """Hash lookup with wrong project returns None."""
        ctx = _make_context(test_context)
        payload = MemoryCandidateCreate(
            project_id=test_project,
            source_type=CandidateSourceType.manual,
            candidate_text="Project-specific candidate.",
        )
        created = submit_candidate(db, ctx, payload=payload)

        fetched = get_candidate_by_hash(
            db,
            project_id=test_project_2,
            candidate_hash=created.candidate_hash,
        )
        assert fetched is None

    def test_get_by_hash_not_found_wrong_hash(self, db, test_project):
        """Hash lookup with non-existent hash returns None."""
        fetched = get_candidate_by_hash(
            db,
            project_id=test_project,
            candidate_hash="a" * 64,
        )
        assert fetched is None


# ═══════════════════════════════════════════════════════════════════════════
# 3. List Candidates
# ═══════════════════════════════════════════════════════════════════════════


class TestListCandidates:
    """GET /api/v4/memory/candidates — list with filters and pagination."""

    def test_list_empty(self, db):
        """Listing candidates with no matches returns empty."""
        unique_project = uuid4()
        items, total = list_candidates(db, project_id=unique_project)
        assert items == []
        assert total == 0

    def test_list_with_items(self, db, test_context, test_project):
        """List returns created candidates."""
        for i in range(3):
            ctx = _make_context(test_context)
            payload = MemoryCandidateCreate(
                project_id=test_project,
                source_type=CandidateSourceType.manual,
                candidate_text=f"Candidate number {i}.",
            )
            submit_candidate(db, ctx, payload=payload)

        items, total = list_candidates(db, project_id=test_project)
        assert len(items) == 3
        assert total == 3

    def test_list_filter_by_source_type(self, db, test_context, test_project):
        """List filtered by source_type returns only matching candidates."""
        for st in [CandidateSourceType.manual, CandidateSourceType.message]:
            ctx = _make_context(test_context)
            payload = MemoryCandidateCreate(
                project_id=test_project,
                source_type=st,
                candidate_text=f"Candidate from {st.value}.",
            )
            submit_candidate(db, ctx, payload=payload)

        items, total = list_candidates(db, project_id=test_project, source_type="manual")
        assert len(items) == 1
        assert total == 1
        assert items[0].source_type == "manual"

    def test_list_filter_by_candidate_status(self, db, test_context, test_project):
        """List filtered by candidate_status returns only matching candidates."""
        ctx = _make_context(test_context)
        payload = MemoryCandidateCreate(
            project_id=test_project,
            source_type=CandidateSourceType.manual,
            candidate_text="Pending candidate.",
        )
        created = submit_candidate(db, ctx, payload=payload)

        update_candidate_status(
            db,
            candidate_id=created.candidate_id,
            from_status="pending_review",
            to_status="approved",
        )

        ctx2 = _make_context(test_context)
        payload2 = MemoryCandidateCreate(
            project_id=test_project,
            source_type=CandidateSourceType.manual,
            candidate_text="Still pending.",
        )
        submit_candidate(db, ctx2, payload=payload2)

        items, total = list_candidates(db, project_id=test_project, candidate_status="approved")
        assert len(items) == 1
        assert total == 1
        assert items[0].candidate_status == "approved"

        items2, total2 = list_candidates(db, project_id=test_project, candidate_status="pending_review")
        assert len(items2) == 1
        assert total2 == 1
        assert items2[0].candidate_status == "pending_review"

    def test_list_pagination(self, db, test_context, test_project):
        """List respects page and page_size."""
        for i in range(5):
            ctx = _make_context(test_context)
            payload = MemoryCandidateCreate(
                project_id=test_project,
                source_type=CandidateSourceType.manual,
                candidate_text=f"Pagination test candidate {i}.",
            )
            submit_candidate(db, ctx, payload=payload)

        items_p1, total = list_candidates(db, project_id=test_project, page=1, page_size=3)
        assert len(items_p1) == 3
        assert total == 5

        items_p2, total2 = list_candidates(db, project_id=test_project, page=2, page_size=3)
        assert len(items_p2) == 2
        assert total2 == 5

    def test_list_ordered_by_created_at_desc(self, db, test_context, test_project):
        """List returns candidates ordered by created_at DESC."""
        for i in range(3):
            ctx = _make_context(test_context)
            payload = MemoryCandidateCreate(
                project_id=test_project,
                source_type=CandidateSourceType.manual,
                candidate_text=f"Order test {i}.",
            )
            submit_candidate(db, ctx, payload=payload)

        items, total = list_candidates(db, project_id=test_project)
        assert total == 3
        if items[0].created_at and items[1].created_at:
            assert items[0].created_at >= items[1].created_at
        if items[1].created_at and items[2].created_at:
            assert items[1].created_at >= items[2].created_at

    def test_list_cross_project_isolation(self, db, test_context, test_project, test_project_2):
        """Candidates from one project don't appear in another project's list."""
        ctx = _make_context(test_context)
        payload = MemoryCandidateCreate(
            project_id=test_project,
            source_type=CandidateSourceType.manual,
            candidate_text="Project A candidate.",
        )
        submit_candidate(db, ctx, payload=payload)

        ctx2 = _make_context(test_context)
        payload2 = MemoryCandidateCreate(
            project_id=test_project_2,
            source_type=CandidateSourceType.manual,
            candidate_text="Project B candidate.",
        )
        submit_candidate(db, ctx2, payload=payload2)

        items_a, total_a = list_candidates(db, project_id=test_project)
        assert total_a == 1
        assert items_a[0].candidate_text == "Project A candidate."

        items_b, total_b = list_candidates(db, project_id=test_project_2)
        assert total_b == 1
        assert items_b[0].candidate_text == "Project B candidate."

    def test_list_page_size_larger_than_total(self, db, test_context, test_project):
        """List with page_size greater than total returns all items."""
        for i in range(2):
            ctx = _make_context(test_context)
            payload = MemoryCandidateCreate(
                project_id=test_project,
                source_type=CandidateSourceType.manual,
                candidate_text=f"Small set {i}.",
            )
            submit_candidate(db, ctx, payload=payload)

        items, total = list_candidates(db, project_id=test_project, page=1, page_size=100)
        assert len(items) == 2
        assert total == 2


# ═══════════════════════════════════════════════════════════════════════════
# 4. Update Candidate
# ═══════════════════════════════════════════════════════════════════════════


class TestUpdateCandidate:
    """PATCH /api/v4/memory/candidates/{id} — update mutable fields."""

    def test_update_title(self, db, test_context, test_project):
        """Update the title of a candidate."""
        ctx = _make_context(test_context)
        payload = MemoryCandidateCreate(
            project_id=test_project,
            source_type=CandidateSourceType.manual,
            candidate_text="Original text.",
            title="Original Title",
        )
        created = submit_candidate(db, ctx, payload=payload)

        ctx2 = _make_context(test_context)
        update_payload = MemoryCandidateUpdate(title="Updated Title")
        updated = update_candidate(db, ctx2, candidate_id=created.candidate_id, payload=update_payload)

        assert updated.title == "Updated Title"
        assert updated.candidate_text == "Original text."

    def test_update_candidate_text(self, db, test_context, test_project):
        """Update the candidate_text of a candidate."""
        ctx = _make_context(test_context)
        payload = MemoryCandidateCreate(
            project_id=test_project,
            source_type=CandidateSourceType.manual,
            candidate_text="Original text.",
        )
        created = submit_candidate(db, ctx, payload=payload)

        ctx2 = _make_context(test_context)
        update_payload = MemoryCandidateUpdate(candidate_text="Updated text.")
        updated = update_candidate(db, ctx2, candidate_id=created.candidate_id, payload=update_payload)

        assert updated.candidate_text == "Updated text."

    def test_update_sensitivity_level(self, db, test_context, test_project):
        """Update the sensitivity_level of a candidate."""
        ctx = _make_context(test_context)
        payload = MemoryCandidateCreate(
            project_id=test_project,
            source_type=CandidateSourceType.manual,
            candidate_text="Sensitive info.",
            sensitivity_level="normal",
        )
        created = submit_candidate(db, ctx, payload=payload)

        ctx2 = _make_context(test_context)
        update_payload = MemoryCandidateUpdate(sensitivity_level="secret")
        updated = update_candidate(db, ctx2, candidate_id=created.candidate_id, payload=update_payload)

        assert updated.sensitivity_level == "secret"

    def test_update_confidence_score(self, db, test_context, test_project):
        """Update the confidence_score of a candidate."""
        ctx = _make_context(test_context)
        payload = MemoryCandidateCreate(
            project_id=test_project,
            source_type=CandidateSourceType.agent_submission,
            candidate_text="Confidence test.",
            confidence_score=0.5,
        )
        created = submit_candidate(db, ctx, payload=payload)

        ctx2 = _make_context(test_context)
        update_payload = MemoryCandidateUpdate(confidence_score=0.95)
        updated = update_candidate(db, ctx2, candidate_id=created.candidate_id, payload=update_payload)

        assert updated.confidence_score == 0.95

    def test_update_metadata_json(self, db, test_context, test_project):
        """Update the metadata_json of a candidate."""
        ctx = _make_context(test_context)
        payload = MemoryCandidateCreate(
            project_id=test_project,
            source_type=CandidateSourceType.manual,
            candidate_text="Metadata test.",
            metadata_json={"key1": "value1"},
        )
        created = submit_candidate(db, ctx, payload=payload)

        ctx2 = _make_context(test_context)
        update_payload = MemoryCandidateUpdate(metadata_json={"key2": "value2", "new": True})
        updated = update_candidate(db, ctx2, candidate_id=created.candidate_id, payload=update_payload)

        assert updated.metadata_json == {"key2": "value2", "new": True}

    def test_update_multiple_fields(self, db, test_context, test_project):
        """Update multiple fields at once."""
        ctx = _make_context(test_context)
        payload = MemoryCandidateCreate(
            project_id=test_project,
            source_type=CandidateSourceType.manual,
            candidate_text="Original.",
            title="Old Title",
            sensitivity_level="normal",
        )
        created = submit_candidate(db, ctx, payload=payload)

        ctx2 = _make_context(test_context)
        update_payload = MemoryCandidateUpdate(
            title="New Title",
            candidate_text="Updated content.",
            sensitivity_level="sensitive",
        )
        updated = update_candidate(db, ctx2, candidate_id=created.candidate_id, payload=update_payload)

        assert updated.title == "New Title"
        assert updated.candidate_text == "Updated content."
        assert updated.sensitivity_level == "sensitive"

    def test_update_nonexistent(self, db, test_context):
        """Updating a non-existent candidate raises ValueError."""
        ctx = _make_context(test_context)
        update_payload = MemoryCandidateUpdate(title="Won't work")
        with pytest.raises(ValueError, match="not found"):
            update_candidate(
                db,
                ctx,
                candidate_id=UUID("99999999-9999-9999-9999-999999999999"),
                payload=update_payload,
            )

    def test_update_partial_no_changes(self, db, test_context, test_project):
        """Update with no fields specified returns the existing candidate unchanged."""
        ctx = _make_context(test_context)
        payload = MemoryCandidateCreate(
            project_id=test_project,
            source_type=CandidateSourceType.manual,
            candidate_text="Unchanged text.",
            title="Unchanged Title",
        )
        created = submit_candidate(db, ctx, payload=payload)

        ctx2 = _make_context(test_context)
        update_payload = MemoryCandidateUpdate()
        updated = update_candidate(db, ctx2, candidate_id=created.candidate_id, payload=update_payload)

        assert updated.title == "Unchanged Title"
        assert updated.candidate_text == "Unchanged text."

    def test_update_candidate_hash_does_not_change(self, db, test_context, test_project):
        """Updating fields does NOT change candidate_hash (hash is only set on create)."""
        ctx = _make_context(test_context)
        payload = MemoryCandidateCreate(
            project_id=test_project,
            source_type=CandidateSourceType.manual,
            candidate_text="Hash stability test.",
            title="Hash Title",
        )
        created = submit_candidate(db, ctx, payload=payload)
        original_hash = created.candidate_hash

        ctx2 = _make_context(test_context)
        update_payload = MemoryCandidateUpdate(title="New Hash Title", candidate_text="New text for hash.")
        updated = update_candidate(db, ctx2, candidate_id=created.candidate_id, payload=update_payload)

        assert updated.candidate_hash == original_hash


# ═══════════════════════════════════════════════════════════════════════════
# 5. Status Transitions
# ═══════════════════════════════════════════════════════════════════════════


class TestCandidateStatusTransitions:
    """Status transitions for memory candidates."""

    def test_approve_from_pending_review(self, db, test_context, test_project):
        """Approve a candidate: pending_review → approved."""
        ctx = _make_context(test_context)
        payload = MemoryCandidateCreate(
            project_id=test_project,
            source_type=CandidateSourceType.manual,
            candidate_text="Approvable candidate.",
        )
        created = submit_candidate(db, ctx, payload=payload)
        assert created.candidate_status == "pending_review"

        result = update_candidate_status(
            db,
            candidate_id=created.candidate_id,
            from_status="pending_review",
            to_status="approved",
        )
        assert result is not None
        assert result.candidate_status == "approved"

        fetched = get_candidate_by_id(db, created.candidate_id)
        assert fetched is not None
        assert fetched.candidate_status == "approved"

    def test_reject_from_pending_review(self, db, test_context, test_project):
        """Reject a candidate: pending_review → rejected."""
        ctx = _make_context(test_context)
        payload = MemoryCandidateCreate(
            project_id=test_project,
            source_type=CandidateSourceType.manual,
            candidate_text="Rejectable candidate.",
        )
        created = submit_candidate(db, ctx, payload=payload)
        assert created.candidate_status == "pending_review"

        result = update_candidate_status(
            db,
            candidate_id=created.candidate_id,
            from_status="pending_review",
            to_status="rejected",
        )
        assert result is not None
        assert result.candidate_status == "rejected"

    def test_approve_already_approved_returns_none(self, db, test_context, test_project):
        """Status transition fails if candidate is not in expected from_status."""
        ctx = _make_context(test_context)
        payload = MemoryCandidateCreate(
            project_id=test_project,
            source_type=CandidateSourceType.manual,
            candidate_text="Already approved.",
        )
        created = submit_candidate(db, ctx, payload=payload)

        update_candidate_status(
            db,
            candidate_id=created.candidate_id,
            from_status="pending_review",
            to_status="approved",
        )

        result = update_candidate_status(
            db,
            candidate_id=created.candidate_id,
            from_status="pending_review",
            to_status="approved",
        )
        assert result is None

    def test_reject_already_rejected_returns_none(self, db, test_context, test_project):
        """Rejecting an already rejected candidate returns None."""
        ctx = _make_context(test_context)
        payload = MemoryCandidateCreate(
            project_id=test_project,
            source_type=CandidateSourceType.manual,
            candidate_text="Already rejected.",
        )
        created = submit_candidate(db, ctx, payload=payload)

        update_candidate_status(
            db,
            candidate_id=created.candidate_id,
            from_status="pending_review",
            to_status="rejected",
        )

        result = update_candidate_status(
            db,
            candidate_id=created.candidate_id,
            from_status="pending_review",
            to_status="rejected",
        )
        assert result is None

    def test_approve_from_rejected_returns_none(self, db, test_context, test_project):
        """Cannot transition from rejected to approved (invalid state jump)."""
        ctx = _make_context(test_context)
        payload = MemoryCandidateCreate(
            project_id=test_project,
            source_type=CandidateSourceType.manual,
            candidate_text="Rejected first.",
        )
        created = submit_candidate(db, ctx, payload=payload)

        update_candidate_status(
            db,
            candidate_id=created.candidate_id,
            from_status="pending_review",
            to_status="rejected",
        )

        result = update_candidate_status(
            db,
            candidate_id=created.candidate_id,
            from_status="rejected",
            to_status="approved",
        )
        assert result is None

    def test_transition_to_conflict(self, db, test_context, test_project):
        """Mark candidate as conflict: pending_review → conflict."""
        ctx = _make_context(test_context)
        payload = MemoryCandidateCreate(
            project_id=test_project,
            source_type=CandidateSourceType.manual,
            candidate_text="Conflicting candidate.",
        )
        created = submit_candidate(db, ctx, payload=payload)

        result = update_candidate_status(
            db,
            candidate_id=created.candidate_id,
            from_status="pending_review",
            to_status="conflict",
        )
        assert result is not None
        assert result.candidate_status == "conflict"

    def test_transition_to_superseded(self, db, test_context, test_project):
        """Mark candidate as superseded: pending_review → superseded."""
        ctx = _make_context(test_context)
        payload = MemoryCandidateCreate(
            project_id=test_project,
            source_type=CandidateSourceType.manual,
            candidate_text="Superseded candidate.",
        )
        created = submit_candidate(db, ctx, payload=payload)

        result = update_candidate_status(
            db,
            candidate_id=created.candidate_id,
            from_status="pending_review",
            to_status="superseded",
        )
        assert result is not None
        assert result.candidate_status == "superseded"

    def test_nonexistent_candidate_status_transition(self, db):
        """Status transition on non-existent candidate returns None."""
        result = update_candidate_status(
            db,
            candidate_id=UUID("99999999-9999-9999-9999-999999999999"),
            from_status="pending_review",
            to_status="approved",
        )
        assert result is None

    def test_full_status_lifecycle(self, db, test_context, test_project):
        """Multiple candidates go through different status paths."""
        ctx1 = _make_context(test_context)
        c1 = submit_candidate(db, ctx1, payload=MemoryCandidateCreate(
            project_id=test_project,
            source_type=CandidateSourceType.manual,
            candidate_text="Candidate 1.",
        ))
        update_candidate_status(db, candidate_id=c1.candidate_id, from_status="pending_review", to_status="approved")

        ctx2 = _make_context(test_context)
        c2 = submit_candidate(db, ctx2, payload=MemoryCandidateCreate(
            project_id=test_project,
            source_type=CandidateSourceType.manual,
            candidate_text="Candidate 2.",
        ))
        update_candidate_status(db, candidate_id=c2.candidate_id, from_status="pending_review", to_status="rejected")

        ctx3 = _make_context(test_context)
        c3 = submit_candidate(db, ctx3, payload=MemoryCandidateCreate(
            project_id=test_project,
            source_type=CandidateSourceType.manual,
            candidate_text="Candidate 3.",
        ))
        update_candidate_status(db, candidate_id=c3.candidate_id, from_status="pending_review", to_status="conflict")

        assert get_candidate_by_id(db, c1.candidate_id).candidate_status == "approved"
        assert get_candidate_by_id(db, c2.candidate_id).candidate_status == "rejected"
        assert get_candidate_by_id(db, c3.candidate_id).candidate_status == "conflict"

        approved_items, approved_total = list_candidates(db, project_id=test_project, candidate_status="approved")
        assert approved_total == 1


# ═══════════════════════════════════════════════════════════════════════════
# 6. Delete Candidate
# ═══════════════════════════════════════════════════════════════════════════


class TestDeleteCandidate:
    """DELETE /api/v4/memory/candidates/{id} — hard delete candidate."""

    def test_delete_existing(self, db, test_context, test_project):
        """Delete an existing candidate succeeds."""
        ctx = _make_context(test_context)
        payload = MemoryCandidateCreate(
            project_id=test_project,
            source_type=CandidateSourceType.manual,
            candidate_text="Deletable candidate.",
        )
        created = submit_candidate(db, ctx, payload=payload)

        ctx2 = _make_context(test_context)
        deleted = delete_candidate(db, ctx2, candidate_id=created.candidate_id)

        assert deleted is not None
        assert deleted.candidate_id == created.candidate_id

        fetched = get_candidate_by_id(db, created.candidate_id)
        assert fetched is None

    def test_delete_nonexistent(self, db, test_context):
        """Deleting a non-existent candidate returns None."""
        ctx = _make_context(test_context)
        deleted = delete_candidate(
            db,
            ctx,
            candidate_id=UUID("99999999-9999-9999-9999-999999999999"),
        )
        assert deleted is None

    def test_delete_and_recreate_with_same_hash(self, db, test_context, test_project):
        """After deleting, the same candidate can be re-submitted (hash is freed)."""
        ctx = _make_context(test_context)
        payload = MemoryCandidateCreate(
            project_id=test_project,
            source_type=CandidateSourceType.manual,
            candidate_text="Recreatable candidate.",
            title="Recreate Test",
        )
        created = submit_candidate(db, ctx, payload=payload)
        original_hash = created.candidate_hash

        ctx2 = _make_context(test_context)
        delete_candidate(db, ctx2, candidate_id=created.candidate_id)

        ctx3 = _make_context(test_context)
        payload2 = MemoryCandidateCreate(
            project_id=test_project,
            source_type=CandidateSourceType.manual,
            candidate_text="Recreatable candidate.",
            title="Recreate Test",
        )
        recreated = submit_candidate(db, ctx3, payload=payload2)

        assert recreated.candidate_id != created.candidate_id
        assert recreated.candidate_hash == original_hash


# ═══════════════════════════════════════════════════════════════════════════
# 7. Idempotency — Dedup via UNIQUE(project_id, candidate_hash)
# ═══════════════════════════════════════════════════════════════════════════


class TestCandidateIdempotency:
    """Idempotency / dedup guarantees for memory_candidates."""

    def test_duplicate_submit_same_hash_returns_existing(self, db, test_context, test_project):
        """Submitting the same candidate twice returns the first one (dedup)."""
        ctx = _make_context(test_context)
        payload = MemoryCandidateCreate(
            project_id=test_project,
            source_type=CandidateSourceType.manual,
            candidate_text="Duplicate candidate.",
            title="Dedup Test",
        )
        c1 = submit_candidate(db, ctx, payload=payload)

        ctx2 = _make_context(test_context)
        c2 = submit_candidate(db, ctx2, payload=payload)

        assert c1.candidate_id == c2.candidate_id
        assert c1.candidate_hash == c2.candidate_hash
        assert c1.created_at == c2.created_at

    def test_duplicate_with_different_payload_same_hash_returns_first(self, db, test_context, test_project):
        """First write wins — different payload metadata with same hash returns first."""
        ctx = _make_context(test_context)
        payload = MemoryCandidateCreate(
            project_id=test_project,
            source_type=CandidateSourceType.manual,
            candidate_text="Same hash different fields.",
            title="First Write",
            sensitivity_level="normal",
        )
        c1 = submit_candidate(db, ctx, payload=payload)

        ctx2 = _make_context(test_context)
        payload2 = MemoryCandidateCreate(
            project_id=test_project,
            source_type=CandidateSourceType.manual,
            candidate_text="Same hash different fields.",
            title="First Write",
            sensitivity_level="secret",
        )
        c2 = submit_candidate(db, ctx2, payload=payload2)

        assert c1.candidate_id == c2.candidate_id
        assert c2.sensitivity_level == "normal"

    def test_same_hash_different_project_allowed(self, db, test_context, test_project, test_project_2):
        """Same candidate_hash in different projects is allowed (UNIQUE is per project)."""
        ctx = _make_context(test_context)
        payload = MemoryCandidateCreate(
            project_id=test_project,
            source_type=CandidateSourceType.manual,
            candidate_text="Cross-project test.",
            title="Shared Content",
        )
        c1 = submit_candidate(db, ctx, payload=payload)

        ctx2 = _make_context(test_context)
        payload2 = MemoryCandidateCreate(
            project_id=test_project_2,
            source_type=CandidateSourceType.manual,
            candidate_text="Cross-project test.",
            title="Shared Content",
        )
        c2 = submit_candidate(db, ctx2, payload=payload2)

        assert c1.candidate_hash == c2.candidate_hash
        assert c1.candidate_id != c2.candidate_id
        assert c1.project_id != c2.project_id

    def test_same_hash_null_project_dedup(self, db, test_context):
        """Multiple candidates with null project_id and same hash are deduped."""
        ctx = _make_context(test_context)
        payload = MemoryCandidateCreate(
            source_type=CandidateSourceType.manual,
            candidate_text="No project dedup.",
        )
        c1 = submit_candidate(db, ctx, payload=payload)

        ctx2 = _make_context(test_context)
        c2 = submit_candidate(db, ctx2, payload=payload)

        assert c1.candidate_id == c2.candidate_id

    def test_different_text_produces_different_hash_no_collision(self, db, test_context, test_project):
        """Different candidate_text → different hash → both stored."""
        ctx1 = _make_context(test_context)
        payload1 = MemoryCandidateCreate(
            project_id=test_project,
            source_type=CandidateSourceType.manual,
            candidate_text="First unique candidate.",
        )
        c1 = submit_candidate(db, ctx1, payload=payload1)

        ctx2 = _make_context(test_context)
        payload2 = MemoryCandidateCreate(
            project_id=test_project,
            source_type=CandidateSourceType.manual,
            candidate_text="Second unique candidate.",
        )
        c2 = submit_candidate(db, ctx2, payload=payload2)

        assert c1.candidate_hash != c2.candidate_hash
        assert c1.candidate_id != c2.candidate_id

    def test_same_idempotency_key_works_with_dedup(self, db, test_context, test_project):
        """Using the same idempotency key for identical submissions returns same candidate."""
        key = str(uuid4())
        ctx1 = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=key,
        )
        payload = MemoryCandidateCreate(
            project_id=test_project,
            source_type=CandidateSourceType.manual,
            candidate_text="Same key dedup.",
        )
        c1 = submit_candidate(db, ctx1, payload=payload)

        ctx2 = RequestContext(
            request_id=test_context.request_id,
            correlation_id=test_context.correlation_id,
            actor=test_context.actor,
            idempotency_key=key,
        )
        c2 = submit_candidate(db, ctx2, payload=payload)

        assert c1.candidate_id == c2.candidate_id


# ═══════════════════════════════════════════════════════════════════════════
# 8. Edge Cases & Constraints
# ═══════════════════════════════════════════════════════════════════════════


class TestCandidateEdgeCases:
    """Edge cases and constraint tests."""

    def test_all_sensitivity_levels(self, db, test_context, test_project):
        """All sensitivity_level values are accepted."""
        levels = ["public", "normal", "private", "sensitive", "secret"]
        for level in levels:
            ctx = _make_context(test_context)
            payload = MemoryCandidateCreate(
                project_id=test_project,
                source_type=CandidateSourceType.manual,
                candidate_text=f"Level {level} candidate.",
                sensitivity_level=level,
            )
            candidate = submit_candidate(db, ctx, payload=payload)
            assert candidate.sensitivity_level == level

    def test_unicode_candidate_text(self, db, test_context, test_project):
        """Unicode characters in candidate_text are handled correctly."""
        ctx = _make_context(test_context)
        payload = MemoryCandidateCreate(
            project_id=test_project,
            source_type=CandidateSourceType.manual,
            candidate_text="你好世界 🌍 日本語テスト 한국어 테스트",
            title="Unicode 记忆标题",
        )
        candidate = submit_candidate(db, ctx, payload=payload)

        assert candidate.title == "Unicode 记忆标题"
        assert "你好世界" in candidate.candidate_text
        assert candidate.candidate_hash is not None
        assert len(candidate.candidate_hash) == 64

    def test_long_candidate_text(self, db, test_context, test_project):
        """Very long candidate_text is handled correctly."""
        long_text = "This is a very long memory candidate. " * 200
        ctx = _make_context(test_context)
        payload = MemoryCandidateCreate(
            project_id=test_project,
            source_type=CandidateSourceType.manual,
            candidate_text=long_text,
        )
        candidate = submit_candidate(db, ctx, payload=payload)

        assert candidate.candidate_text == long_text
        assert candidate.candidate_hash is not None

    def test_long_title(self, db, test_context, test_project):
        """Title up to 240 characters is accepted."""
        long_title = "T" * 240
        ctx = _make_context(test_context)
        payload = MemoryCandidateCreate(
            project_id=test_project,
            source_type=CandidateSourceType.manual,
            candidate_text="Long title test.",
            title=long_title,
        )
        candidate = submit_candidate(db, ctx, payload=payload)
        assert candidate.title == long_title

    def test_complex_metadata_json(self, db, test_context, test_project):
        """Complex nested metadata_json is handled correctly."""
        ctx = _make_context(test_context)
        complex_metadata = {
            "nested": {"key": "value", "list": [1, 2, 3]},
            "tags": ["important", "review"],
            "source": {"type": "message", "id": str(uuid4()), "confidence": 0.9},
        }
        payload = MemoryCandidateCreate(
            project_id=test_project,
            source_type=CandidateSourceType.agent_submission,
            candidate_text="Complex metadata.",
            metadata_json=complex_metadata,
        )
        candidate = submit_candidate(db, ctx, payload=payload)

        assert candidate.metadata_json == complex_metadata

    def test_empty_candidate_text_min_length_one(self, db, test_context, test_project):
        """candidate_text with a single character is accepted."""
        ctx = _make_context(test_context)
        payload = MemoryCandidateCreate(
            project_id=test_project,
            source_type=CandidateSourceType.manual,
            candidate_text="X",
        )
        candidate = submit_candidate(db, ctx, payload=payload)
        assert candidate.candidate_text == "X"

    def test_source_id_null(self, db, test_context, test_project):
        """source_id can be None."""
        ctx = _make_context(test_context)
        payload = MemoryCandidateCreate(
            project_id=test_project,
            source_type=CandidateSourceType.importer,
            source_id=None,
            candidate_text="No source ID.",
        )
        candidate = submit_candidate(db, ctx, payload=payload)
        assert candidate.source_id is None

    def test_source_id_explicit(self, db, test_context, test_project):
        """source_id with explicit UUID is stored."""
        sid = uuid4()
        ctx = _make_context(test_context)
        payload = MemoryCandidateCreate(
            project_id=test_project,
            source_type=CandidateSourceType.message,
            source_id=sid,
            candidate_text="With source ID.",
        )
        candidate = submit_candidate(db, ctx, payload=payload)
        assert candidate.source_id == sid

    def test_confidence_score_none(self, db, test_context, test_project):
        """confidence_score can be None (manual submission default)."""
        ctx = _make_context(test_context)
        payload = MemoryCandidateCreate(
            project_id=test_project,
            source_type=CandidateSourceType.manual,
            candidate_text="No confidence.",
        )
        candidate = submit_candidate(db, ctx, payload=payload)
        assert candidate.confidence_score is None

    def test_confidence_score_high_precision(self, db, test_context, test_project):
        """Confidence score with high precision (4 decimal places) is stored."""
        ctx = _make_context(test_context)
        payload = MemoryCandidateCreate(
            project_id=test_project,
            source_type=CandidateSourceType.agent_submission,
            candidate_text="Precision test.",
            confidence_score=0.9876,
        )
        candidate = submit_candidate(db, ctx, payload=payload)
        assert candidate.confidence_score == 0.9876

    def test_multiple_candidates_same_project(self, db, test_context, test_project):
        """Multiple different candidates in the same project are all stored."""
        for i in range(10):
            ctx = _make_context(test_context)
            payload = MemoryCandidateCreate(
                project_id=test_project,
                source_type=CandidateSourceType.manual,
                candidate_text=f"Candidate {i} with unique content.",
            )
            submit_candidate(db, ctx, payload=payload)

        items, total = list_candidates(db, project_id=test_project)
        assert total == 10

    def test_candidate_created_at_is_set(self, db, test_context, test_project):
        """created_at is automatically set on creation."""
        ctx = _make_context(test_context)
        payload = MemoryCandidateCreate(
            project_id=test_project,
            source_type=CandidateSourceType.manual,
            candidate_text="Timestamp test.",
        )
        candidate = submit_candidate(db, ctx, payload=payload)

        assert candidate.created_at is not None
        assert candidate.updated_at is not None

    def test_list_large_page_size(self, db, test_context, test_project):
        """List with page=1, page_size=200 returns correct items."""
        for i in range(5):
            ctx = _make_context(test_context)
            payload = MemoryCandidateCreate(
                project_id=test_project,
                source_type=CandidateSourceType.manual,
                candidate_text=f"Page size test {i}.",
            )
            submit_candidate(db, ctx, payload=payload)

        items, total = list_candidates(db, project_id=test_project, page=1, page_size=200)
        assert len(items) == 5
        assert total == 5

    def test_list_page_2_empty(self, db, test_context, test_project):
        """Page beyond available data returns empty list with correct total."""
        for i in range(2):
            ctx = _make_context(test_context)
            payload = MemoryCandidateCreate(
                project_id=test_project,
                source_type=CandidateSourceType.manual,
                candidate_text=f"Empty page test {i}.",
            )
            submit_candidate(db, ctx, payload=payload)

        items, total = list_candidates(db, project_id=test_project, page=2, page_size=10)
        assert len(items) == 0
        assert total == 2

    def test_list_default_page(self, db, test_context, test_project):
        """List without explicit page parameters defaults to page=1, page_size=50."""
        for i in range(3):
            ctx = _make_context(test_context)
            payload = MemoryCandidateCreate(
                project_id=test_project,
                source_type=CandidateSourceType.manual,
                candidate_text=f"Default page test {i}.",
            )
            submit_candidate(db, ctx, payload=payload)

        items, total = list_candidates(db, project_id=test_project)
        assert len(items) == 3
        assert total == 3
