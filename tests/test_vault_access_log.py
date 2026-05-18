"""P2-09 Vault access log tests — write, query, filtering, edge cases.

Covers:
1. Write access log entries for all 8 valid actions.
2. Query access logs with pagination.
3. Filter by action and result.
4. Filter by time range.
5. Denied/failed logs include reason_code.
6. Access log entries do NOT contain plaintext credentials.
7. Access log helper handles missing optional fields.
8. Concurrent writes produce distinct log entries.
9. Empty result set when no logs match filters.

These tests require a running PostgreSQL with the full 45-table schema
and a configured ``DATABASE_URL``.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from mneme.db.base import SessionLocal
from mneme.db.vault import get_vault_access_logs
from mneme.vault.access_log import (
    VaultAccessAction,
    VaultAccessResult,
    write_vault_access_log,
)

# ─────────────────────────────────────────────────────────────────────────────
# Test infrastructure — create provider + credential for FK satisfaction
# ─────────────────────────────────────────────────────────────────────────────

_TEST_PROVIDER_CODE = "p2_09_test_provider"


def _ensure_test_provider() -> UUID:
    """Return the provider_id of a reusable test provider (create if needed)."""
    with SessionLocal() as db:
        existing = db.execute(
            text("SELECT provider_id FROM providers WHERE provider_code = :code"),
            {"code": _TEST_PROVIDER_CODE},
        ).scalar()
        if existing:
            return existing

        pid = db.execute(
            text(
                "INSERT INTO providers (provider_code, name, provider_type) "
                "VALUES (:code, :name, :ptype) RETURNING provider_id"
            ),
            {"code": _TEST_PROVIDER_CODE, "name": "P2-09 Test Provider", "ptype": "llm"},
        ).scalar_one()
        db.commit()
        return pid


def _create_test_credential(*, credential_type: str = "api_key") -> tuple[UUID, UUID]:
    """Create a minimal credential row for access-log testing.

    Returns ``(credential_id, provider_id)``.
    """
    provider_id = _ensure_test_provider()
    cname = f"test-cred-{uuid4().hex[:8]}"

    # Fake ciphertext / key_wrap (not used for access log tests)
    fake_bytes = os.urandom(32)

    with SessionLocal() as db:
        cid = db.execute(
            text(
                "INSERT INTO credential_vault "
                "(provider_id, credential_name, credential_type, ciphertext, "
                " key_wrap, key_version, fingerprint) "
                "VALUES (:pid, :cname, :ctype, :ct, :kw, :kv, :fp) "
                "RETURNING credential_id"
            ),
            {
                "pid": provider_id,
                "cname": cname,
                "ctype": credential_type,
                "ct": fake_bytes,
                "kw": fake_bytes,
                "kv": "v1",
                "fp": "test-fingerprint",
            },
        ).scalar_one()
        db.commit()
        return cid, provider_id


def _delete_test_credential(credential_id: UUID) -> None:
    """Clean up a test credential and its access logs."""
    with SessionLocal() as db:
        db.execute(
            text("DELETE FROM vault_access_logs WHERE credential_id = :cid"),
            {"cid": credential_id},
        )
        db.execute(
            text("DELETE FROM credential_vault WHERE credential_id = :cid"),
            {"cid": credential_id},
        )
        db.commit()


def _get_all_logs_for_credential(credential_id: UUID) -> list[dict]:
    """Return all access log rows for a credential as dicts (chronological)."""
    with SessionLocal() as db:
        rows = (
            db.execute(
                text(
                    "SELECT * FROM vault_access_logs "
                    "WHERE credential_id = :cid ORDER BY occurred_at ASC"
                ),
                {"cid": credential_id},
            )
            .mappings()
            .all()
        )
        return [dict(r) for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# 1. Write access log — all valid actions
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "action,result_val",
    [
        ("create", "success"),
        ("enable", "success"),
        ("disable", "success"),
        ("rotate", "success"),
        ("revoke", "success"),
        ("export", "success"),
        ("use", "success"),
        ("access_denied", "denied"),
        ("access_denied", "failed"),
        ("use", "denied"),
        ("rotate", "failed"),
    ],
)
def test_write_access_log_all_actions(action: str, result_val: str) -> None:
    """Each valid action/result pair must produce a row in vault_access_logs."""
    cid, pid = _create_test_credential()
    aid = uuid4()
    rid = uuid4()

    try:
        log_id = write_vault_access_log(
            credential_id=cid,
            actor_type="user",
            actor_id=aid,
            auth_context_type="user_session",
            auth_context_id=aid,
            action=action,
            result=result_val,
            provider_id=pid,
            request_id=rid,
            correlation_id=rid,
            reason_code="test_reason" if result_val != "success" else None,
        )

        assert isinstance(log_id, UUID)

        # Verify row exists with correct values
        with SessionLocal() as db:
            row = (
                db.execute(
                    text("SELECT * FROM vault_access_logs WHERE access_log_id = :lid"),
                    {"lid": log_id},
                )
                .mappings()
                .first()
            )
        assert row is not None, f"Log entry not found for action={action}"
        assert row["action"] == action
        assert row["result"] == result_val
        assert row["credential_id"] == cid
        assert row["actor_type"] == "user"
        if result_val != "success":
            assert row["reason_code"] == "test_reason"
    finally:
        _delete_test_credential(cid)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Denied / failed logs include reason_code
# ─────────────────────────────────────────────────────────────────────────────


def test_access_denied_log_has_reason_code() -> None:
    cid, pid = _create_test_credential()
    try:
        write_vault_access_log(
            credential_id=cid,
            actor_type="system",
            action="access_denied",
            result="denied",
            reason_code="credential_revoked",
        )
        logs = _get_all_logs_for_credential(cid)
        assert len(logs) == 1
        assert logs[0]["action"] == "access_denied"
        assert logs[0]["result"] == "denied"
        assert logs[0]["reason_code"] == "credential_revoked"
    finally:
        _delete_test_credential(cid)


def test_access_failed_log_has_reason_code() -> None:
    cid, pid = _create_test_credential()
    try:
        write_vault_access_log(
            credential_id=cid,
            actor_type="system",
            action="access_denied",
            result="failed",
            reason_code="decryption_failed",
        )
        logs = _get_all_logs_for_credential(cid)
        assert len(logs) == 1
        assert logs[0]["result"] == "failed"
        assert logs[0]["reason_code"] == "decryption_failed"
    finally:
        _delete_test_credential(cid)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Access log entries do NOT contain plaintext
# ─────────────────────────────────────────────────────────────────────────────


def test_access_log_never_stores_ciphertext_or_plaintext_fields() -> None:
    cid, pid = _create_test_credential()
    try:
        write_vault_access_log(
            credential_id=cid,
            actor_type="system",
            action="use",
            result="success",
        )
        logs = _get_all_logs_for_credential(cid)
        log = logs[0]
        forbidden = {"ciphertext", "key_wrap", "plaintext", "secret", "password"}
        for key in forbidden:
            assert key not in log, f"Access log should never contain '{key}'"
    finally:
        _delete_test_credential(cid)


def test_metadata_json_is_present_and_structured() -> None:
    cid, pid = _create_test_credential()
    try:
        write_vault_access_log(
            credential_id=cid,
            actor_type="user",
            action="create",
            result="success",
            metadata_json={"description": "test credential"},
        )
        logs = _get_all_logs_for_credential(cid)
        assert len(logs) == 1
        assert isinstance(logs[0]["metadata_json"], dict)
        assert isinstance(logs[0]["target_scope"], dict)
    finally:
        _delete_test_credential(cid)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Query access logs — pagination
# ─────────────────────────────────────────────────────────────────────────────


def test_query_access_logs_pagination() -> None:
    cid, pid = _create_test_credential()
    try:
        for _ in range(15):
            write_vault_access_log(
                credential_id=cid, actor_type="user", action="use", result="success",
            )

        rows, total = get_vault_access_logs(credential_id=cid, page=1, page_size=5)
        assert total == 15
        assert len(rows) == 5

        rows, total = get_vault_access_logs(credential_id=cid, page=2, page_size=5)
        assert total == 15
        assert len(rows) == 5

        rows, total = get_vault_access_logs(credential_id=cid, page=3, page_size=5)
        assert total == 15
        assert len(rows) == 5

        rows, total = get_vault_access_logs(credential_id=cid, page=4, page_size=5)
        assert total == 15
        assert len(rows) == 0
    finally:
        _delete_test_credential(cid)


def test_query_access_logs_default_pagination() -> None:
    cid, pid = _create_test_credential()
    try:
        write_vault_access_log(
            credential_id=cid, actor_type="user", action="use", result="success"
        )
        rows, total = get_vault_access_logs(credential_id=cid)
        assert total >= 1
        assert len(rows) >= 1
    finally:
        _delete_test_credential(cid)


# ─────────────────────────────────────────────────────────────────────────────
# 5. Query access logs — filter by action
# ─────────────────────────────────────────────────────────────────────────────


def test_query_access_logs_filter_by_action() -> None:
    cid, pid = _create_test_credential()
    try:
        write_vault_access_log(credential_id=cid, actor_type="user", action="create", result="success")
        write_vault_access_log(credential_id=cid, actor_type="user", action="use", result="success")
        write_vault_access_log(credential_id=cid, actor_type="user", action="use", result="success")
        write_vault_access_log(credential_id=cid, actor_type="user", action="rotate", result="success")

        rows, total = get_vault_access_logs(credential_id=cid, action="use")
        assert total == 2
        for row in rows:
            assert row["action"] == "use"

        rows, total = get_vault_access_logs(credential_id=cid, action="create")
        assert total == 1

        rows, total = get_vault_access_logs(credential_id=cid, action="revoke")
        assert total == 0
    finally:
        _delete_test_credential(cid)


# ─────────────────────────────────────────────────────────────────────────────
# 6. Query access logs — filter by result
# ─────────────────────────────────────────────────────────────────────────────


def test_query_access_logs_filter_by_result() -> None:
    cid, pid = _create_test_credential()
    try:
        write_vault_access_log(credential_id=cid, actor_type="user", action="use",
                               result="success")
        write_vault_access_log(credential_id=cid, actor_type="user", action="use",
                               result="denied", reason_code="permission")
        write_vault_access_log(credential_id=cid, actor_type="user", action="access_denied",
                               result="failed", reason_code="error")

        rows, total = get_vault_access_logs(credential_id=cid, result="success")
        assert total == 1
        assert rows[0]["result"] == "success"

        rows, total = get_vault_access_logs(credential_id=cid, result="denied")
        assert total == 1
        assert rows[0]["reason_code"] == "permission"

        rows, total = get_vault_access_logs(credential_id=cid, result="failed")
        assert total == 1
        assert rows[0]["reason_code"] == "error"
    finally:
        _delete_test_credential(cid)


# ─────────────────────────────────────────────────────────────────────────────
# 7. Query access logs — combined action + result filter
# ─────────────────────────────────────────────────────────────────────────────


def test_query_access_logs_filter_by_action_and_result() -> None:
    cid, pid = _create_test_credential()
    try:
        write_vault_access_log(credential_id=cid, actor_type="user", action="use",
                               result="success")
        write_vault_access_log(credential_id=cid, actor_type="user", action="use",
                               result="denied", reason_code="no_permission")
        write_vault_access_log(credential_id=cid, actor_type="user", action="rotate",
                               result="success")

        rows, total = get_vault_access_logs(credential_id=cid, action="use", result="success")
        assert total == 1

        rows, total = get_vault_access_logs(credential_id=cid, action="use", result="denied")
        assert total == 1
        assert rows[0]["reason_code"] == "no_permission"

        rows, total = get_vault_access_logs(credential_id=cid, action="rotate", result="denied")
        assert total == 0
    finally:
        _delete_test_credential(cid)


# ─────────────────────────────────────────────────────────────────────────────
# 8. Query access logs — time range filter
# ─────────────────────────────────────────────────────────────────────────────


def test_query_access_logs_filter_by_time_range() -> None:
    cid, pid = _create_test_credential()
    try:
        write_vault_access_log(
            credential_id=cid, actor_type="user", action="create", result="success"
        )
        now = datetime.now(timezone.utc)

        rows, total = get_vault_access_logs(
            credential_id=cid, occurred_after=now - timedelta(minutes=5)
        )
        assert total >= 1

        rows, total = get_vault_access_logs(
            credential_id=cid, occurred_before=now + timedelta(minutes=5)
        )
        assert total >= 1

        rows, total = get_vault_access_logs(
            credential_id=cid, occurred_after=now + timedelta(hours=1)
        )
        assert total == 0

        rows, total = get_vault_access_logs(
            credential_id=cid, occurred_before=now - timedelta(hours=1)
        )
        assert total == 0
    finally:
        _delete_test_credential(cid)


# ─────────────────────────────────────────────────────────────────────────────
# 9. Edge cases — missing optional fields
# ─────────────────────────────────────────────────────────────────────────────


def test_write_log_minimal_fields() -> None:
    cid, pid = _create_test_credential()
    try:
        log_id = write_vault_access_log(
            credential_id=cid, action="use", result="success",
        )
        assert isinstance(log_id, UUID)

        logs = _get_all_logs_for_credential(cid)
        log = logs[0]
        assert log["action"] == "use"
        assert log["result"] == "success"
        assert log["actor_type"] == "system"  # default
        assert log["actor_id"] is None
    finally:
        _delete_test_credential(cid)


def test_write_log_without_credential_id() -> None:
    """Writing a log with credential_id=None must succeed (nullable FK)."""
    log_id = write_vault_access_log(
        credential_id=None,
        actor_type="user",
        action="export",
        result="success",
    )
    assert isinstance(log_id, UUID)

    with SessionLocal() as db:
        row = (
            db.execute(
                text("SELECT * FROM vault_access_logs WHERE access_log_id = :lid"),
                {"lid": log_id},
            )
            .mappings()
            .first()
        )
        assert row is not None
        assert row["credential_id"] is None
        assert row["action"] == "export"


# ─────────────────────────────────────────────────────────────────────────────
# 10. Multiple credentials — isolation
# ─────────────────────────────────────────────────────────────────────────────


def test_access_logs_are_scoped_to_credential() -> None:
    cid_a, _ = _create_test_credential()
    cid_b, _ = _create_test_credential()
    try:
        write_vault_access_log(credential_id=cid_a, action="create", result="success")
        write_vault_access_log(credential_id=cid_b, action="rotate", result="success")
        write_vault_access_log(credential_id=cid_a, action="use", result="success")

        rows_a, total_a = get_vault_access_logs(credential_id=cid_a)
        rows_b, total_b = get_vault_access_logs(credential_id=cid_b)

        assert total_a == 2
        assert total_b == 1
        for row in rows_a:
            assert UUID(row["credential_id"]) == cid_a
    finally:
        _delete_test_credential(cid_a)
        _delete_test_credential(cid_b)


# ─────────────────────────────────────────────────────────────────────────────
# 11. Ordering — most recent first
# ─────────────────────────────────────────────────────────────────────────────


def test_access_logs_ordered_by_occurred_at_desc() -> None:
    cid, pid = _create_test_credential()
    try:
        write_vault_access_log(credential_id=cid, action="create", result="success")
        time.sleep(0.05)
        write_vault_access_log(credential_id=cid, action="use", result="success")
        time.sleep(0.05)
        write_vault_access_log(credential_id=cid, action="rotate", result="success")

        rows, total = get_vault_access_logs(credential_id=cid)
        assert total == 3
        assert rows[0]["action"] == "rotate"
        assert rows[1]["action"] == "use"
        assert rows[2]["action"] == "create"
    finally:
        _delete_test_credential(cid)


# ─────────────────────────────────────────────────────────────────────────────
# 12. Empty result set
# ─────────────────────────────────────────────────────────────────────────────


def test_query_access_logs_empty_for_unknown_credential() -> None:
    unknown_cid = uuid4()
    rows, total = get_vault_access_logs(credential_id=unknown_cid)
    assert total == 0
    assert len(rows) == 0


def test_query_access_logs_empty_with_mismatched_filter() -> None:
    cid, pid = _create_test_credential()
    try:
        write_vault_access_log(credential_id=cid, action="create", result="success")
        rows, total = get_vault_access_logs(credential_id=cid, action="revoke")
        assert total == 0
    finally:
        _delete_test_credential(cid)


# ─────────────────────────────────────────────────────────────────────────────
# 13. Access log row structure
# ─────────────────────────────────────────────────────────────────────────────


def test_access_log_row_has_all_expected_fields() -> None:
    cid, pid = _create_test_credential()
    aid = uuid4()
    rid = uuid4()
    try:
        write_vault_access_log(
            credential_id=cid,
            actor_type="agent",
            actor_id=aid,
            auth_context_type="agent_token",
            auth_context_id=aid,
            action="use",
            result="success",
            capability_id=None,
            provider_id=pid,
            request_id=rid,
            correlation_id=rid,
            target_scope={"project_id": "proj-1"},
            metadata_json={"trace_id": "abc"},
        )

        rows, total = get_vault_access_logs(credential_id=cid)
        assert total == 1
        row = rows[0]

        assert "access_log_id" in row
        assert row["credential_id"] is not None
        assert row["actor_type"] == "agent"
        assert row["actor_id"] is not None
        assert row["auth_context_type"] == "agent_token"
        assert row["auth_context_id"] is not None
        assert row["action"] == "use"
        assert row["result"] == "success"
        assert row["provider_id"] is not None
        assert row["request_id"] is not None
        assert row["correlation_id"] is not None
        assert row["target_scope"] == {"project_id": "proj-1"}
        assert row["metadata_json"] == {"trace_id": "abc"}
        assert row["occurred_at"] is not None
    finally:
        _delete_test_credential(cid)


# ─────────────────────────────────────────────────────────────────────────────
# 14. Concurrent writes produce distinct log IDs
# ─────────────────────────────────────────────────────────────────────────────


def test_concurrent_writes_produce_distinct_log_ids() -> None:
    cid, pid = _create_test_credential()
    try:
        ids = set()
        for _ in range(10):
            log_id = write_vault_access_log(
                credential_id=cid, actor_type="user", action="use", result="success",
            )
            ids.add(log_id)
        assert len(ids) == 10

        rows, total = get_vault_access_logs(credential_id=cid)
        assert total == 10
    finally:
        _delete_test_credential(cid)


# ─────────────────────────────────────────────────────────────────────────────
# 15. Default actor_type is "system"
# ─────────────────────────────────────────────────────────────────────────────


def test_default_actor_type_is_system() -> None:
    cid, pid = _create_test_credential()
    try:
        write_vault_access_log(credential_id=cid, action="use", result="success")
        rows, total = get_vault_access_logs(credential_id=cid)
        assert total == 1
        assert rows[0]["actor_type"] == "system"
    finally:
        _delete_test_credential(cid)
