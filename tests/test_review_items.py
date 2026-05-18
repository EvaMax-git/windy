"""P2-05 Review Items — comprehensive tests for CRUD, state machine, and audit.

Covers:
1. Create review items with all review_type values.
2. List review items with pagination and filters.
3. Get single review item by ID (found / not-found).
4. Claim operation (pending → in_review).
5. Approve operation (in_review → approved).
6. Reject operation (in_review → rejected).
7. Cancel operation (pending/in_review → cancelled).
8. State machine guards: blocked transitions.
9. Idempotency: duplicate create with same key.
10. Schema validation: invalid enums, missing fields.

These tests require a running PostgreSQL with the full 45-table schema
and a configured ``DATABASE_URL``.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from mneme.db.base import SessionLocal
from mneme.db.review_items import (
    approve_review_item,
    cancel_review_item,
    create_review_item,
    get_review_item_by_id,
    get_review_items,
    move_to_in_review,
    reject_review_item,
)
from mneme.schemas.review_items import (
    ReviewDecision,
    ReviewStatus,
    ReviewTargetType,
    ReviewType,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _get_owner_id() -> UUID:
    """Return the bootstrap owner user_id."""
    with SessionLocal() as db:
        row = db.execute(
            text("SELECT user_id FROM users WHERE username = 'owner'")
        ).scalar_one()
        return UUID(str(row))


def _cleanup_review_items(ids: list[UUID]) -> None:
    """Delete review items by ID (test teardown)."""
    if not ids:
        return
    with SessionLocal() as db:
        for rid in ids:
            db.execute(
                text("DELETE FROM review_items WHERE review_item_id = :id"),
                {"id": str(rid)},
            )
        db.commit()


def _create_test_review_item(
    review_type: str = "manual",
    target_type: str = "job",
    target_id: UUID | None = None,
    project_id: UUID | None = None,
    priority: int = 100,
    requester_actor_type: str = "system",
    requester_actor_id: UUID | None = None,
    idempotency_key: str | None = None,
    decision_payload: dict | None = None,
) -> dict:
    """Create a review item with sensible test defaults."""
    return create_review_item(
        project_id=project_id,
        review_type=review_type,
        target_type=target_type,
        target_id=target_id or uuid4(),
        priority=priority,
        requester_actor_type=requester_actor_type,
        requester_actor_id=requester_actor_id,
        decision_payload=decision_payload,
        correlation_id=uuid4(),
        request_id=uuid4(),
        idempotency_key=idempotency_key or str(uuid4()),
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. Create review items
# ─────────────────────────────────────────────────────────────────────────────


class TestCreateReviewItem:
    """Create operations via the DB layer."""

    def test_create_minimal(self):
        """Create a review item with only required fields."""
        row = _create_test_review_item()
        rid = UUID(row["review_item_id"])
        try:
            assert row["status"] == "pending"
            assert row["review_type"] == "manual"
            assert row["target_type"] == "job"
            assert row["priority"] == 100
            assert row["requester_actor_type"] == "system"
            assert row["decision_payload"] == {}
        finally:
            _cleanup_review_items([rid])

    def test_create_all_review_types(self):
        """Every review_type enum value can be used."""
        ids = []
        try:
            for rt in ReviewType:
                row = _create_test_review_item(
                    review_type=rt.value,
                    idempotency_key=str(uuid4()),
                )
                ids.append(UUID(row["review_item_id"]))
                assert row["review_type"] == rt.value
        finally:
            _cleanup_review_items(ids)

    def test_create_all_target_types(self):
        """Every target_type enum value can be used."""
        ids = []
        try:
            for tt in ReviewTargetType:
                row = _create_test_review_item(
                    review_type="manual",
                    target_type=tt.value,
                    idempotency_key=str(uuid4()),
                )
                ids.append(UUID(row["review_item_id"]))
                assert row["target_type"] == tt.value
        finally:
            _cleanup_review_items(ids)

    def test_create_with_decision_payload(self):
        """decision_payload JSONB is stored correctly."""
        payload = {"risk_score": 85, "tags": ["urgent", "write"]}
        row = _create_test_review_item(
            decision_payload=payload,
            idempotency_key=str(uuid4()),
        )
        rid = UUID(row["review_item_id"])
        try:
            assert row["decision_payload"] == payload
        finally:
            _cleanup_review_items([rid])

    def test_create_with_requester_info(self):
        """requester_actor_type and requester_actor_id are stored."""
        owner_id = _get_owner_id()
        row = _create_test_review_item(
            requester_actor_type="user",
            requester_actor_id=owner_id,
            idempotency_key=str(uuid4()),
        )
        rid = UUID(row["review_item_id"])
        try:
            assert row["requester_actor_type"] == "user"
            assert UUID(row["requester_actor_id"]) == owner_id
        finally:
            _cleanup_review_items([rid])

    def test_create_idempotency_conflict(self):
        """Duplicate idempotency_key raises IntegrityError."""
        key = str(uuid4())
        row = _create_test_review_item(idempotency_key=key)
        rid = UUID(row["review_item_id"])
        try:
            with pytest.raises(IntegrityError):
                _create_test_review_item(idempotency_key=key)
        finally:
            _cleanup_review_items([rid])

    def test_create_minimal_priority_via_db_check(self):
        """Priority 0 is valid (DB CHECK: 0–1000)."""
        row = _create_test_review_item(
            priority=0,
            idempotency_key=str(uuid4()),
        )
        rid = UUID(row["review_item_id"])
        try:
            assert row["priority"] == 0
        finally:
            _cleanup_review_items([rid])

    def test_create_max_priority_via_db_check(self):
        """Priority 1000 is valid (DB CHECK: 0–1000)."""
        row = _create_test_review_item(
            priority=1000,
            idempotency_key=str(uuid4()),
        )
        rid = UUID(row["review_item_id"])
        try:
            assert row["priority"] == 1000
        finally:
            _cleanup_review_items([rid])

    def test_create_priority_out_of_range_raises(self):
        """Priority 1001 should be rejected by DB CHECK constraint."""
        with pytest.raises(IntegrityError):
            _create_test_review_item(
                priority=1001,
                idempotency_key=str(uuid4()),
            )


# ─────────────────────────────────────────────────────────────────────────────
# 2. List review items
# ─────────────────────────────────────────────────────────────────────────────


class TestListReviewItems:
    """Pagination and filtering via the DB layer."""

    def setup_method(self):
        self.ids = []
        for i in range(5):
            row = _create_test_review_item(
                review_type="manual",
                idempotency_key=f"list-test-{uuid4()}",
            )
            self.ids.append(UUID(row["review_item_id"]))

    def teardown_method(self):
        _cleanup_review_items(self.ids)

    def test_list_filter_correct_type(self):
        """Listing with a review_type filter returns only matching rows."""
        # All items created in setup have review_type="manual"
        rows, _ = get_review_items(
            page=1, page_size=50, review_type="manual",
        )
        assert isinstance(rows, list)
        for r in rows:
            assert r["review_type"] == "manual"

    def test_pagination_page1(self):
        """Page 1 returns at most page_size items."""
        rows, total = get_review_items(page=1, page_size=3)
        assert total >= 5
        assert len(rows) <= 3

    def test_pagination_page2_no_overlap(self):
        """Pages do not overlap."""
        rows1, total1 = get_review_items(page=1, page_size=3)
        rows2, total2 = get_review_items(page=2, page_size=3)
        assert total1 == total2
        ids1 = {r["review_item_id"] for r in rows1}
        ids2 = {r["review_item_id"] for r in rows2}
        assert ids1.isdisjoint(ids2)

    def test_filter_by_review_type(self):
        """Filter by review_type returns only matching rows."""
        rows, _ = get_review_items(
            page=1, page_size=100, review_type="manual",
        )
        for r in rows:
            assert r["review_type"] == "manual"

    def test_filter_by_status(self):
        """Filter by status returns only matching rows."""
        move_to_in_review(self.ids[0])
        rows, total = get_review_items(
            page=1, page_size=100, status="in_review",
        )
        assert total >= 1
        for r in rows:
            assert r["status"] == "in_review"

    def test_filter_by_target_type(self):
        """Filter by target_type returns only matching rows."""
        rows, _ = get_review_items(
            page=1, page_size=100, target_type="job",
        )
        for r in rows:
            assert r["target_type"] == "job"

    def test_list_returns_all_fields(self):
        """Returned dicts contain every top-level field expected."""
        rows, _ = get_review_items(page=1, page_size=1)
        assert len(rows) > 0
        row = rows[0]
        for field in (
            "review_item_id", "review_type", "target_type", "target_id",
            "status", "priority", "requester_actor_type",
            "correlation_id", "request_id", "idempotency_key",
            "created_at",
        ):
            assert field in row, f"Missing field: {field}"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Get single review item
# ─────────────────────────────────────────────────────────────────────────────


class TestGetReviewItem:
    """Single-item lookup by primary key."""

    def test_get_existing(self):
        """Fetching an existing item returns its data."""
        row = _create_test_review_item()
        rid = UUID(row["review_item_id"])
        try:
            item = get_review_item_by_id(rid)
            assert item is not None
            assert item["review_item_id"] == str(rid)
            assert item["status"] == "pending"
        finally:
            _cleanup_review_items([rid])

    def test_get_not_found(self):
        """Fetching a non-existent ID returns None."""
        item = get_review_item_by_id(uuid4())
        assert item is None


# ─────────────────────────────────────────────────────────────────────────────
# 4. Claim (pending → in_review)
# ─────────────────────────────────────────────────────────────────────────────


class TestClaimReviewItem:
    """Move review items from pending to in_review."""

    def test_claim_from_pending(self):
        """A pending item can be claimed (→ in_review)."""
        row = _create_test_review_item()
        rid = UUID(row["review_item_id"])
        try:
            ok = move_to_in_review(rid)
            assert ok is True
            item = get_review_item_by_id(rid)
            assert item["status"] == "in_review"
        finally:
            _cleanup_review_items([rid])

    def test_claim_already_in_review_fails(self):
        """An in_review item cannot be claimed again."""
        row = _create_test_review_item()
        rid = UUID(row["review_item_id"])
        try:
            move_to_in_review(rid)
            ok = move_to_in_review(rid)
            assert ok is False
        finally:
            _cleanup_review_items([rid])

    def test_claim_approved_fails(self):
        """An approved item cannot be claimed."""
        row = _create_test_review_item()
        rid = UUID(row["review_item_id"])
        try:
            move_to_in_review(rid)
            approve_review_item(rid, _get_owner_id())
            ok = move_to_in_review(rid)
            assert ok is False
        finally:
            _cleanup_review_items([rid])

    def test_claim_rejected_fails(self):
        """A rejected item cannot be claimed."""
        row = _create_test_review_item()
        rid = UUID(row["review_item_id"])
        try:
            move_to_in_review(rid)
            reject_review_item(rid, _get_owner_id())
            ok = move_to_in_review(rid)
            assert ok is False
        finally:
            _cleanup_review_items([rid])

    def test_claim_cancelled_fails(self):
        """A cancelled item cannot be claimed."""
        row = _create_test_review_item()
        rid = UUID(row["review_item_id"])
        try:
            cancel_review_item(rid)
            ok = move_to_in_review(rid)
            assert ok is False
        finally:
            _cleanup_review_items([rid])


# ─────────────────────────────────────────────────────────────────────────────
# 5. Approve (in_review → approved)
# ─────────────────────────────────────────────────────────────────────────────


class TestApproveReviewItem:
    """Approve review items."""

    def test_approve_from_in_review(self):
        """An in_review item can be approved."""
        row = _create_test_review_item()
        rid = UUID(row["review_item_id"])
        try:
            move_to_in_review(rid)
            ok = approve_review_item(rid, _get_owner_id(), reason="Verified")
            assert ok is True
            item = get_review_item_by_id(rid)
            assert item["status"] == "approved"
            assert item["decision"] == "approved"
            assert item["reason"] == "Verified"
            assert item["reviewer_id"] is not None
            assert item["decided_at"] is not None
        finally:
            _cleanup_review_items([rid])

    def test_approve_from_pending_fails(self):
        """A pending item cannot be approved directly."""
        row = _create_test_review_item()
        rid = UUID(row["review_item_id"])
        try:
            ok = approve_review_item(rid, _get_owner_id())
            assert ok is False
        finally:
            _cleanup_review_items([rid])

    def test_approve_already_approved_fails(self):
        """An already-approved item cannot be approved again."""
        row = _create_test_review_item()
        rid = UUID(row["review_item_id"])
        try:
            move_to_in_review(rid)
            approve_review_item(rid, _get_owner_id())
            ok = approve_review_item(rid, _get_owner_id())
            assert ok is False
        finally:
            _cleanup_review_items([rid])

    def test_approve_rejected_fails(self):
        """A rejected item cannot be approved."""
        row = _create_test_review_item()
        rid = UUID(row["review_item_id"])
        try:
            move_to_in_review(rid)
            reject_review_item(rid, _get_owner_id())
            ok = approve_review_item(rid, _get_owner_id())
            assert ok is False
        finally:
            _cleanup_review_items([rid])


# ─────────────────────────────────────────────────────────────────────────────
# 6. Reject (in_review → rejected)
# ─────────────────────────────────────────────────────────────────────────────


class TestRejectReviewItem:
    """Reject review items."""

    def test_reject_from_in_review(self):
        """An in_review item can be rejected."""
        row = _create_test_review_item()
        rid = UUID(row["review_item_id"])
        try:
            move_to_in_review(rid)
            ok = reject_review_item(rid, _get_owner_id(), reason="Needs work")
            assert ok is True
            item = get_review_item_by_id(rid)
            assert item["status"] == "rejected"
            assert item["decision"] == "rejected"
            assert item["reason"] == "Needs work"
            assert item["decided_at"] is not None
        finally:
            _cleanup_review_items([rid])

    def test_reject_from_pending_fails(self):
        """A pending item cannot be rejected directly."""
        row = _create_test_review_item()
        rid = UUID(row["review_item_id"])
        try:
            ok = reject_review_item(rid, _get_owner_id())
            assert ok is False
        finally:
            _cleanup_review_items([rid])

    def test_reject_already_rejected_fails(self):
        """An already-rejected item cannot be rejected again."""
        row = _create_test_review_item()
        rid = UUID(row["review_item_id"])
        try:
            move_to_in_review(rid)
            reject_review_item(rid, _get_owner_id())
            ok = reject_review_item(rid, _get_owner_id())
            assert ok is False
        finally:
            _cleanup_review_items([rid])


# ─────────────────────────────────────────────────────────────────────────────
# 7. Cancel (pending / in_review → cancelled)
# ─────────────────────────────────────────────────────────────────────────────


class TestCancelReviewItem:
    """Cancel review items from pending or in_review status."""

    def test_cancel_from_pending(self):
        """A pending item can be cancelled."""
        row = _create_test_review_item()
        rid = UUID(row["review_item_id"])
        try:
            ok = cancel_review_item(rid)
            assert ok is True
            item = get_review_item_by_id(rid)
            assert item["status"] == "cancelled"
            assert item["decision"] == "cancelled"
            assert item["decided_at"] is not None
        finally:
            _cleanup_review_items([rid])

    def test_cancel_from_in_review(self):
        """An in_review item can be cancelled."""
        row = _create_test_review_item()
        rid = UUID(row["review_item_id"])
        try:
            move_to_in_review(rid)
            ok = cancel_review_item(rid)
            assert ok is True
            item = get_review_item_by_id(rid)
            assert item["status"] == "cancelled"
            assert item["decision"] == "cancelled"
        finally:
            _cleanup_review_items([rid])

    def test_cancel_approved_fails(self):
        """An approved item cannot be cancelled."""
        row = _create_test_review_item()
        rid = UUID(row["review_item_id"])
        try:
            move_to_in_review(rid)
            approve_review_item(rid, _get_owner_id())
            ok = cancel_review_item(rid)
            assert ok is False
        finally:
            _cleanup_review_items([rid])

    def test_cancel_rejected_fails(self):
        """A rejected item cannot be cancelled."""
        row = _create_test_review_item()
        rid = UUID(row["review_item_id"])
        try:
            move_to_in_review(rid)
            reject_review_item(rid, _get_owner_id())
            ok = cancel_review_item(rid)
            assert ok is False
        finally:
            _cleanup_review_items([rid])


# ─────────────────────────────────────────────────────────────────────────────
# 8. State machine integrity (full transition matrix)
# ─────────────────────────────────────────────────────────────────────────────


class TestStateMachineIntegrity:
    """Verify the complete state transition matrix.

    Valid transitions:
    - pending → in_review (claim)
    - pending → cancelled (cancel)
    - in_review → approved (approve)
    - in_review → rejected (reject)
    - in_review → cancelled (cancel)

    Blocked transitions (all others):
    - pending → approved (blocked)
    - pending → rejected (blocked)
    - approved → * (blocked)
    - rejected → * (blocked)
    - cancelled → * (blocked)
    """

    def _make_and_cleanup(self, *, setup_fn=None):
        """Helper: create item, run setup, return (row, rid)."""
        row = _create_test_review_item()
        rid = UUID(row["review_item_id"])
        if setup_fn:
            setup_fn(rid)
        return row, rid

    def test_full_happy_path(self):
        """pending → in_review → approved."""
        row = _create_test_review_item()
        rid = UUID(row["review_item_id"])
        try:
            assert row["status"] == "pending"
            ok = move_to_in_review(rid)
            assert ok
            item = get_review_item_by_id(rid)
            assert item["status"] == "in_review"
            ok = approve_review_item(rid, _get_owner_id(), reason="OK")
            assert ok
            item = get_review_item_by_id(rid)
            assert item["status"] == "approved"
            assert item["decision"] == "approved"
        finally:
            _cleanup_review_items([rid])

    def test_reject_path(self):
        """pending → in_review → rejected."""
        row = _create_test_review_item()
        rid = UUID(row["review_item_id"])
        try:
            move_to_in_review(rid)
            ok = reject_review_item(rid, _get_owner_id(), reason="Nope")
            assert ok
            item = get_review_item_by_id(rid)
            assert item["status"] == "rejected"
            assert item["decision"] == "rejected"
        finally:
            _cleanup_review_items([rid])

    def test_cancel_from_pending_path(self):
        """pending → cancelled."""
        row = _create_test_review_item()
        rid = UUID(row["review_item_id"])
        try:
            ok = cancel_review_item(rid)
            assert ok
            item = get_review_item_by_id(rid)
            assert item["status"] == "cancelled"
        finally:
            _cleanup_review_items([rid])

    def test_cancel_from_in_review_path(self):
        """pending → in_review → cancelled."""
        row = _create_test_review_item()
        rid = UUID(row["review_item_id"])
        try:
            move_to_in_review(rid)
            ok = cancel_review_item(rid)
            assert ok
            item = get_review_item_by_id(rid)
            assert item["status"] == "cancelled"
        finally:
            _cleanup_review_items([rid])

    def test_no_reverse_approved_to_anything(self):
        """approved items cannot transition to any other state."""
        row = _create_test_review_item()
        rid = UUID(row["review_item_id"])
        try:
            move_to_in_review(rid)
            approve_review_item(rid, _get_owner_id())
            assert not move_to_in_review(rid)
            assert not approve_review_item(rid, _get_owner_id())
            assert not reject_review_item(rid, _get_owner_id())
            assert not cancel_review_item(rid)
        finally:
            _cleanup_review_items([rid])

    def test_no_reverse_rejected_to_anything(self):
        """rejected items cannot transition to any other state."""
        row = _create_test_review_item()
        rid = UUID(row["review_item_id"])
        try:
            move_to_in_review(rid)
            reject_review_item(rid, _get_owner_id())
            assert not move_to_in_review(rid)
            assert not approve_review_item(rid, _get_owner_id())
            assert not reject_review_item(rid, _get_owner_id())
            assert not cancel_review_item(rid)
        finally:
            _cleanup_review_items([rid])

    def test_no_reverse_cancelled_to_anything(self):
        """cancelled items cannot transition to any other state."""
        row = _create_test_review_item()
        rid = UUID(row["review_item_id"])
        try:
            cancel_review_item(rid)
            assert not move_to_in_review(rid)
            assert not approve_review_item(rid, _get_owner_id())
            assert not reject_review_item(rid, _get_owner_id())
            assert not cancel_review_item(rid)
        finally:
            _cleanup_review_items([rid])

    def test_pending_cannot_be_approved_directly(self):
        """pending → approved is blocked (must go through in_review)."""
        row = _create_test_review_item()
        rid = UUID(row["review_item_id"])
        try:
            ok = approve_review_item(rid, _get_owner_id())
            assert ok is False
        finally:
            _cleanup_review_items([rid])

    def test_pending_cannot_be_rejected_directly(self):
        """pending → rejected is blocked (must go through in_review)."""
        row = _create_test_review_item()
        rid = UUID(row["review_item_id"])
        try:
            ok = reject_review_item(rid, _get_owner_id())
            assert ok is False
        finally:
            _cleanup_review_items([rid])


# ─────────────────────────────────────────────────────────────────────────────
# 9. Schema validation
# ─────────────────────────────────────────────────────────────────────────────


class TestReviewSchemas:
    """Pydantic schema validation for review_items."""

    def test_review_type_enum_values(self):
        """ReviewType enum has all 7 values."""
        values = {rt.value for rt in ReviewType}
        expected = {
            "memory_candidate", "sensitive_access", "high_cost_call",
            "import_confirm", "restore_confirm", "dlq_replay", "manual",
        }
        assert values == expected

    def test_review_status_enum_values(self):
        """ReviewStatus enum has all 6 values."""
        values = {rs.value for rs in ReviewStatus}
        expected = {
            "pending", "in_review", "approved",
            "rejected", "cancelled", "expired",
        }
        assert values == expected

    def test_review_target_type_enum_values(self):
        """ReviewTargetType enum has all 9 values."""
        values = {tt.value for tt in ReviewTargetType}
        expected = {
            "memory_candidate", "memory", "asset", "job",
            "dead_letter", "provider_call", "credential",
            "import_run", "restore_run",
        }
        assert values == expected

    def test_review_decision_enum_values(self):
        """ReviewDecision enum has 4 values."""
        values = {rd.value for rd in ReviewDecision}
        expected = {"approved", "rejected", "cancelled", "expired"}
        assert values == expected

    def test_schema_construct_read(self):
        """ReviewItemRead can be constructed with required fields."""
        from mneme.schemas.review_items import ReviewItemRead

        read = ReviewItemRead(
            review_item_id=uuid4(),
            review_type="manual",
            target_type="job",
            target_id=uuid4(),
            status="pending",
            priority=100,
            requester_actor_type="system",
            correlation_id=uuid4(),
            request_id=uuid4(),
            idempotency_key="test-key",
        )
        assert read.status == "pending"
        assert read.review_type == "manual"

    def test_schema_construct_create(self):
        """ReviewItemCreate can be constructed with required fields."""
        from mneme.schemas.review_items import ReviewItemCreate

        create = ReviewItemCreate(
            review_type=ReviewType.manual,
            target_type=ReviewTargetType.job,
            target_id=uuid4(),
            priority=100,
        )
        assert create.review_type == ReviewType.manual
        assert create.target_type == ReviewTargetType.job
        assert create.priority == 100

    def test_requester_actor_type_enum(self):
        """RequesterActorType has all 4 values."""
        from mneme.schemas.review_items import RequesterActorType
        values = {r.value for r in RequesterActorType}
        assert values == {"user", "agent", "service", "system"}
