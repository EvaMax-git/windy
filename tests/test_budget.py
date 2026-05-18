"""P2-13 Budget / Usage Limits tests.

Covers:
1. usage_limits CRUD operations (create, read, list, update, delete).
2. check_budget_allow — no limits, within limits, exceeded limits, block threshold.
3. get_limit_usage — usage aggregation within time window.
4. budget_tracking — reserve / commit / release / deny lifecycle.
5. Schema validation for UsageLimitCreate, UsageLimitRead, etc.
"""

from __future__ import annotations

import math
import time
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from mneme.db.base import SessionLocal
from mneme.db.budget import (
    check_budget_allow,
    create_usage_limit,
    delete_usage_limit,
    get_budget_tracking,
    get_limit_usage,
    get_usage_limit_by_id,
    get_usage_limits,
    reserve_budget,
    transition_budget_state,
    update_usage_limit,
)
from mneme.schemas.gateway import (
    LimitScope,
    LimitSubjectType,
    LimitUsageRead,
    LimitWindowUnit,
    UsageLimitCreate,
    UsageLimitRead,
    UsageLimitUpdate,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _upsert_test_project(db, project_id: UUID) -> None:
    """Ensure a dummy project row exists for FK integrity."""
    from sqlalchemy import text
    db.execute(
        text("""
            INSERT INTO projects (project_id, project_code, name, status)
            VALUES (:pid, :code, 'budget-test-project', 'active')
            ON CONFLICT (project_id) DO NOTHING
        """),
        {"pid": project_id, "code": f"btp_{project_id.hex[:12]}"},
    )
    db.commit()


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def test_project_id() -> UUID:
    pid = uuid4()
    with SessionLocal() as db:
        _upsert_test_project(db, pid)
    return pid


@pytest.fixture
def test_subject_id() -> UUID:
    return uuid4()


@pytest.fixture
def test_subject_type() -> str:
    return "user"


# ---------------------------------------------------------------------------
# usage_limits CRUD
# ---------------------------------------------------------------------------

class TestUsageLimitCrud:
    """Full CRUD lifecycle for usage_limits."""

    def test_create_and_read(self, test_subject_type, test_subject_id):
        """Create a limit then read it back."""
        row = create_usage_limit(
            subject_type=test_subject_type,
            subject_id=test_subject_id,
            limit_scope="global",
            window_unit="day",
            max_requests=1000,
            max_cost=50.0,
            approval_threshold_cost=10.0,
            block_threshold_cost=100.0,
        )
        assert row is not None
        limit_id = UUID(row["usage_limit_id"])

        fetched = get_usage_limit_by_id(limit_id)
        assert fetched is not None
        assert fetched["subject_type"] == test_subject_type
        assert fetched["max_requests"] == 1000
        assert float(fetched["max_cost"]) == 50.0
        assert float(fetched["approval_threshold_cost"]) == 10.0
        assert float(fetched["block_threshold_cost"]) == 100.0
        assert fetched["enabled"] is True
        assert fetched["window_unit"] == "day"

    def test_list_with_filters(self, test_subject_type, test_subject_id):
        """List limits with subject_type filter."""
        # Create a few limits
        for i in range(3):
            create_usage_limit(
                subject_type=test_subject_type,
                subject_id=test_subject_id,
                limit_scope="global",
                window_unit="day",
                max_requests=100 * (i + 1),
            )

        rows, total = get_usage_limits(
            page=1,
            page_size=10,
            subject_type=test_subject_type,
            subject_id=test_subject_id,
        )
        assert total >= 3
        assert len(rows) >= 3

    def test_list_pagination(self, test_subject_type, test_subject_id):
        """Pagination limits work with page/page_size."""
        for i in range(5):
            create_usage_limit(
                subject_type=test_subject_type,
                subject_id=test_subject_id,
                limit_scope="global",
                window_unit="day",
                max_requests=10,
            )

        rows, total = get_usage_limits(
            page=1, page_size=2,
            subject_type=test_subject_type,
            subject_id=test_subject_id,
        )
        assert len(rows) <= 2
        assert total >= 5

    def test_update(self, test_subject_type, test_subject_id):
        """Update an existing limit."""
        row = create_usage_limit(
            subject_type=test_subject_type,
            subject_id=test_subject_id,
            limit_scope="global",
            window_unit="day",
            max_requests=500,
            max_cost=25.0,
        )
        limit_id = UUID(row["usage_limit_id"])

        ok = update_usage_limit(
            usage_limit_id=limit_id,
            max_requests=2000,
            max_cost=100.0,
            enabled=False,
        )
        assert ok is True

        fetched = get_usage_limit_by_id(limit_id)
        assert fetched["max_requests"] == 2000
        assert float(fetched["max_cost"]) == 100.0
        assert fetched["enabled"] is False

    def test_delete(self, test_subject_type, test_subject_id):
        """Delete a limit."""
        row = create_usage_limit(
            subject_type=test_subject_type,
            subject_id=test_subject_id,
            limit_scope="global",
            window_unit="day",
            max_requests=10,
        )
        limit_id = UUID(row["usage_limit_id"])

        ok = delete_usage_limit(limit_id)
        assert ok is True

        fetched = get_usage_limit_by_id(limit_id)
        assert fetched is None

    def test_get_nonexistent_returns_none(self):
        """get_usage_limit_by_id returns None for missing ID."""
        assert get_usage_limit_by_id(uuid4()) is None

    def test_get_limit_usage_no_data(self, test_subject_type, test_subject_id):
        """get_limit_usage returns zeroes when no committed budget exists."""
        row = create_usage_limit(
            subject_type=test_subject_type,
            subject_id=test_subject_id,
            limit_scope="global",
            window_unit="day",
            max_requests=1000,
        )
        limit_id = UUID(row["usage_limit_id"])

        usage = get_limit_usage(limit_id)
        assert usage is not None
        assert usage["total_requests"] == 0
        assert usage["total_committed_cost"] == 0.0

    def test_get_limit_usage_nonexistent(self):
        """get_limit_usage returns None for nonexistent limit."""
        assert get_limit_usage(uuid4()) is None


# ---------------------------------------------------------------------------
# check_budget_allow
# ---------------------------------------------------------------------------

class TestCheckBudgetAllow:
    """Budget enforcement logic."""

    def test_no_limits_allows(self, test_subject_type, test_subject_id):
        """When no usage_limits exist, calls are always allowed."""
        allowed, reason = check_budget_allow(
            subject_type=test_subject_type,
            subject_id=test_subject_id,
            estimated_cost=1000000.0,  # huge cost
        )
        assert allowed is True
        assert reason is None

    def test_within_limits_allows(self, test_subject_type, test_subject_id):
        """Call within limits passes."""
        create_usage_limit(
            subject_type=test_subject_type,
            subject_id=test_subject_id,
            limit_scope="global",
            window_unit="day",
            max_cost=100.0,
        )

        allowed, reason = check_budget_allow(
            subject_type=test_subject_type,
            subject_id=test_subject_id,
            estimated_cost=5.0,
        )
        assert allowed is True
        assert reason is None

    def test_exceeded_max_cost_denies(self, test_subject_type, test_subject_id):
        """Call exceeding max_cost is denied."""
        create_usage_limit(
            subject_type=test_subject_type,
            subject_id=test_subject_id,
            limit_scope="global",
            window_unit="day",
            max_cost=10.0,
        )

        # Reserve and commit some cost first
        budget_id = reserve_budget(
            request_id=uuid4(),
            correlation_id=uuid4(),
            subject_type=test_subject_type,
            subject_id=test_subject_id,
            reserved_cost=9.0,
        )
        transition_budget_state(
            budget_tracking_id=budget_id,
            new_state="committed",
            expected_state="reserved",
            committed_cost=9.0,
        )

        # Now estimated cost of 5.0 would push total to 14.0 > 10.0
        allowed, reason = check_budget_allow(
            subject_type=test_subject_type,
            subject_id=test_subject_id,
            estimated_cost=5.0,
        )
        assert allowed is False
        assert reason is not None
        assert "max_cost" in reason.lower() or "budget exceeded" in reason.lower()

    def test_exceeded_max_requests_denies(self, test_subject_type, test_subject_id):
        """Call exceeding max_requests is denied."""
        create_usage_limit(
            subject_type=test_subject_type,
            subject_id=test_subject_id,
            limit_scope="global",
            window_unit="day",
            max_requests=2,
        )

        # Commit 2 requests
        for _ in range(2):
            b_id = reserve_budget(
                request_id=uuid4(),
                correlation_id=uuid4(),
                subject_type=test_subject_type,
                subject_id=test_subject_id,
                reserved_cost=1.0,
            )
            transition_budget_state(
                budget_tracking_id=b_id,
                new_state="committed",
                expected_state="reserved",
                committed_cost=1.0,
            )

        allowed, reason = check_budget_allow(
            subject_type=test_subject_type,
            subject_id=test_subject_id,
            estimated_cost=0.0,
        )
        assert allowed is False
        assert reason is not None
        assert "max_requests" in reason.lower() or "requests" in reason.lower()

    def test_block_threshold_denies(self, test_subject_type, test_subject_id):
        """Call exceeding block_threshold_cost is denied even without prior usage."""
        create_usage_limit(
            subject_type=test_subject_type,
            subject_id=test_subject_id,
            limit_scope="global",
            window_unit="day",
            block_threshold_cost=50.0,
            max_cost=1000.0,  # plenty of room
        )

        allowed, reason = check_budget_allow(
            subject_type=test_subject_type,
            subject_id=test_subject_id,
            estimated_cost=75.0,  # > 50 block threshold
        )
        assert allowed is False
        assert reason is not None
        assert "block" in reason.lower()

    def test_disabled_limit_ignored(self, test_subject_type, test_subject_id):
        """Disabled limits do not block calls."""
        create_usage_limit(
            subject_type=test_subject_type,
            subject_id=test_subject_id,
            limit_scope="global",
            window_unit="day",
            max_cost=1.0,  # very tight
            enabled=False,
        )

        allowed, reason = check_budget_allow(
            subject_type=test_subject_type,
            subject_id=test_subject_id,
            estimated_cost=500.0,
        )
        assert allowed is True

    def test_approval_threshold_logs_but_allows(self, test_subject_type, test_subject_id):
        """Approval threshold is logged but does NOT deny."""
        create_usage_limit(
            subject_type=test_subject_type,
            subject_id=test_subject_id,
            limit_scope="global",
            window_unit="day",
            approval_threshold_cost=10.0,
            max_cost=500.0,  # high ceiling
        )

        allowed, reason = check_budget_allow(
            subject_type=test_subject_type,
            subject_id=test_subject_id,
            estimated_cost=50.0,  # > approval threshold but < max_cost
        )
        assert allowed is True

    def test_limit_scoped_to_other_subject_ignored(self, test_subject_type, test_subject_id):
        """A limit scoped to a different subject_id doesn't affect this one."""
        other_id = uuid4()
        create_usage_limit(
            subject_type=test_subject_type,
            subject_id=other_id,  # different subject
            limit_scope="global",
            window_unit="day",
            max_cost=1.0,  # very tight for the OTHER subject
        )

        allowed, reason = check_budget_allow(
            subject_type=test_subject_type,
            subject_id=test_subject_id,  # THIS subject
            estimated_cost=500.0,
        )
        assert allowed is True


# ---------------------------------------------------------------------------
# budget_tracking lifecycle
# ---------------------------------------------------------------------------

class TestBudgetTrackingLifecycle:
    """Reserve → commit / release / deny lifecycle."""

    def test_reserve_and_commit(self, test_subject_type, test_subject_id):
        """Full reserve → commit flow."""
        req_id = uuid4()
        corr_id = uuid4()

        budget_id = reserve_budget(
            request_id=req_id,
            correlation_id=corr_id,
            subject_type=test_subject_type,
            subject_id=test_subject_id,
            reserved_cost=15.0,
        )

        row = get_budget_tracking(budget_id)
        assert row is not None
        assert row["reservation_state"] == "reserved"
        assert float(row["reserved_cost"]) == 15.0

        ok = transition_budget_state(
            budget_tracking_id=budget_id,
            new_state="committed",
            expected_state="reserved",
            committed_cost=12.5,
            actual_input_tokens=100,
            actual_output_tokens=50,
        )
        assert ok is True

        row = get_budget_tracking(budget_id)
        assert row["reservation_state"] == "committed"
        assert float(row["committed_cost"]) == 12.5
        assert row["actual_input_tokens"] == 100
        assert row["actual_output_tokens"] == 50

    def test_reserve_and_release(self, test_subject_type, test_subject_id):
        """Reserve then release (e.g., provider timeout)."""
        budget_id = reserve_budget(
            request_id=uuid4(),
            correlation_id=uuid4(),
            subject_type=test_subject_type,
            subject_id=test_subject_id,
            reserved_cost=8.0,
        )

        ok = transition_budget_state(
            budget_tracking_id=budget_id,
            new_state="released",
            expected_state="reserved",
            released_cost=8.0,
        )
        assert ok is True

        row = get_budget_tracking(budget_id)
        assert row["reservation_state"] == "released"

    def test_reserve_and_deny(self, test_subject_type, test_subject_id):
        """Reserve then deny (e.g., credential unavailable)."""
        budget_id = reserve_budget(
            request_id=uuid4(),
            correlation_id=uuid4(),
            subject_type=test_subject_type,
            subject_id=test_subject_id,
            reserved_cost=3.0,
        )

        ok = transition_budget_state(
            budget_tracking_id=budget_id,
            new_state="denied",
            expected_state="reserved",
            denied_reason="credential_unavailable",
        )
        assert ok is True

        row = get_budget_tracking(budget_id)
        assert row["reservation_state"] == "denied"
        assert row["denied_reason"] == "credential_unavailable"

    def test_transition_wrong_expected_state_fails(self, test_subject_type, test_subject_id):
        """Optimistic concurrency: transition fails if current state != expected."""
        budget_id = reserve_budget(
            request_id=uuid4(),
            correlation_id=uuid4(),
            subject_type=test_subject_type,
            subject_id=test_subject_id,
            reserved_cost=5.0,
        )

        # Try to transition as if it were already committed
        ok = transition_budget_state(
            budget_tracking_id=budget_id,
            new_state="refunded",
            expected_state="committed",  # wrong: it's "reserved"
        )
        assert ok is False

        # State should still be "reserved"
        row = get_budget_tracking(budget_id)
        assert row["reservation_state"] == "reserved"

    def test_get_nonexistent_budget_returns_none(self):
        """get_budget_tracking returns None for missing ID."""
        assert get_budget_tracking(uuid4()) is None


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

class TestBudgetSchemas:
    """Pydantic schema validation for budget/limits."""

    def test_usage_limit_create_valid(self):
        """Minimal valid UsageLimitCreate."""
        sid = uuid4()
        obj = UsageLimitCreate(
            subject_type=LimitSubjectType.user,
            subject_id=sid,
            limit_scope=LimitScope.global_,
            window_unit=LimitWindowUnit.day,
            max_requests=100,
            max_cost=10.0,
        )
        data = obj.model_dump()
        assert data["subject_type"] == "user"
        assert data["subject_id"] == sid
        assert data["limit_scope"] == "global"
        assert data["max_requests"] == 100

    def test_usage_limit_read_parses(self):
        """UsageLimitRead can parse a dict from DB layer."""
        sid = str(uuid4())
        lid = str(uuid4())
        row = {
            "usage_limit_id": lid,
            "subject_type": "user",
            "subject_id": sid,
            "capability_id": None,
            "provider_id": None,
            "project_id": None,
            "limit_scope": "global",
            "window_unit": "day",
            "max_requests": 100,
            "max_input_tokens": None,
            "max_output_tokens": None,
            "max_total_tokens": None,
            "max_cost": 10.0,
            "approval_threshold_cost": None,
            "block_threshold_cost": None,
            "enabled": True,
            "created_at": "2026-05-03T00:00:00+00:00",
            "updated_at": "2026-05-03T00:00:00+00:00",
        }
        obj = UsageLimitRead(**row)
        assert obj.usage_limit_id == UUID(lid)
        assert obj.subject_type == "user"
        assert obj.max_cost == 10.0

    def test_usage_limit_update_partial(self):
        """UsageLimitUpdate allows partial updates."""
        obj = UsageLimitUpdate(max_requests=500, enabled=False)
        data = obj.model_dump(exclude_none=True)
        assert data == {"max_requests": 500, "enabled": False}

    def test_limit_usage_read_parses(self):
        """LimitUsageRead can parse usage data."""
        now = datetime.now(timezone.utc)
        row = {
            "usage_limit_id": str(uuid4()),
            "window_unit": "day",
            "window_start": (now - timedelta(days=1)).isoformat(),
            "window_end": now.isoformat(),
            "total_requests": 42,
            "total_input_tokens": 1000,
            "total_output_tokens": 500,
            "total_total_tokens": 1500,
            "total_committed_cost": 2.5,
            "limits": {
                "usage_limit_id": str(uuid4()),
                "subject_type": "user",
                "subject_id": str(uuid4()),
                "limit_scope": "global",
                "window_unit": "day",
                "max_requests": 100,
                "max_input_tokens": None,
                "max_output_tokens": None,
                "max_total_tokens": None,
                "max_cost": 10.0,
                "approval_threshold_cost": None,
                "block_threshold_cost": None,
                "enabled": True,
                "created_at": None,
                "updated_at": None,
                "capability_id": None,
                "provider_id": None,
                "project_id": None,
            },
        }
        obj = LimitUsageRead(**row)
        assert obj.total_requests == 42
        assert obj.total_committed_cost == 2.5
        assert obj.limits.max_cost == 10.0

    def test_invalid_subject_type_rejected(self):
        """Pydantic rejects invalid subject_type."""
        with pytest.raises(ValueError):
            UsageLimitCreate(
                subject_type="invalid_type",  # type: ignore
                subject_id=uuid4(),
            )

    def test_negative_max_requests_rejected(self):
        """Pydantic rejects negative max_requests."""
        with pytest.raises(ValueError):
            UsageLimitCreate(
                subject_type=LimitSubjectType.user,
                subject_id=uuid4(),
                max_requests=-1,
            )

    def test_all_subject_types_allowed(self):
        """All 5 subject_type values are valid."""
        for st in LimitSubjectType:
            obj = UsageLimitCreate(
                subject_type=st,
                subject_id=uuid4(),
            )
            assert obj.subject_type == st

    def test_all_window_units_allowed(self):
        """All 4 window_unit values are valid."""
        for wu in LimitWindowUnit:
            obj = UsageLimitCreate(
                subject_type=LimitSubjectType.user,
                subject_id=uuid4(),
                window_unit=wu,
            )
            assert obj.window_unit == wu

    def test_all_limit_scopes_allowed(self):
        """All 4 limit_scope values are valid."""
        for ls in LimitScope:
            obj = UsageLimitCreate(
                subject_type=LimitSubjectType.user,
                subject_id=uuid4(),
                limit_scope=ls,
            )
            assert obj.limit_scope == ls
