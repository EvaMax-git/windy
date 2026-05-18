"""P2-08/P2-09 Vault data-access layer.

Provides pure-SQL queries against the ``credential_vault`` and
``vault_access_logs`` tables.  All queries use SQLAlchemy ``text()`` so
they align exactly with the DDL column names in
``0001_baseline_45_tables.py``.

**Database compatibility**: Query templates avoid dialect-specific types
(no ``PG_UUID``, ``JSONB``, or ``now()``).  UUIDs are passed as strings,
JSON columns as JSON-encoded strings, and timestamps as Python ``datetime``
objects.  This ensures the same SQL works against both PostgreSQL and SQLite.

State machine
-------------
The ``status`` column follows this state machine:

* ``active`` — credential is usable
* ``disabled`` — credential is temporarily disabled
* ``rotated`` — credential has been replaced; the old row is kept for audit
* ``revoked`` — credential is permanently revoked

Transitions are enforced by application-level guards.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import text

from mneme.db.base import SessionLocal

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# SQL templates — credential_vault
# ═══════════════════════════════════════════════════════════════════════════════

_COUNT_CREDENTIALS = text("""
    SELECT count(*) AS total
    FROM credential_vault
    WHERE 1=1
      AND (:provider_id IS NULL OR provider_id = :provider_id)
      AND (:credential_type IS NULL OR credential_type = :credential_type)
      AND (:status IS NULL OR status = :status)
      AND (:created_after IS NULL OR created_at >= :created_after)
      AND (:created_before IS NULL OR created_at <= :created_before)
""")

_QUERY_CREDENTIALS = text("""
    SELECT
        credential_id,
        provider_id,
        credential_name,
        credential_type,
        status,
        key_version,
        fingerprint,
        scope_json,
        metadata_json,
        rotated_at,
        last_used_at,
        revoked_at,
        created_by_user_id,
        created_at,
        updated_at
    FROM credential_vault
    WHERE 1=1
      AND (:provider_id IS NULL OR provider_id = :provider_id)
      AND (:credential_type IS NULL OR credential_type = :credential_type)
      AND (:status IS NULL OR status = :status)
      AND (:created_after IS NULL OR created_at >= :created_after)
      AND (:created_before IS NULL OR created_at <= :created_before)
    ORDER BY created_at DESC
    LIMIT :limit OFFSET :offset
""")

_GET_CREDENTIAL_BY_ID = text("""
    SELECT
        credential_id,
        provider_id,
        credential_name,
        credential_type,
        status,
        ciphertext,
        key_wrap,
        key_version,
        fingerprint,
        scope_json,
        metadata_json,
        rotated_at,
        last_used_at,
        revoked_at,
        created_by_user_id,
        created_at,
        updated_at
    FROM credential_vault
    WHERE credential_id = :credential_id
""")

_INSERT_CREDENTIAL = text("""
    INSERT INTO credential_vault (
        credential_id,
        provider_id,
        credential_name,
        credential_type,
        status,
        ciphertext,
        key_wrap,
        key_version,
        fingerprint,
        scope_json,
        metadata_json,
        created_by_user_id
    ) VALUES (
        :credential_id,
        :provider_id,
        :credential_name,
        :credential_type,
        :status,
        :ciphertext,
        :key_wrap,
        :key_version,
        :fingerprint,
        :scope_json,
        :metadata_json,
        :created_by_user_id
    )
    RETURNING credential_id
""")

_UPDATE_CREDENTIAL = text("""
    UPDATE credential_vault
    SET
        status = COALESCE(:status, status),
        scope_json = COALESCE(:scope_json, scope_json),
        metadata_json = COALESCE(:metadata_json, metadata_json),
        updated_at = :updated_at
    WHERE credential_id = :credential_id
""")

_UPDATE_CREDENTIAL_WITH_ROTATION = text("""
    UPDATE credential_vault
    SET
        ciphertext = :ciphertext,
        key_wrap = :key_wrap,
        key_version = :key_version,
        fingerprint = :fingerprint,
        status = COALESCE(:status, status),
        scope_json = COALESCE(:scope_json, scope_json),
        metadata_json = COALESCE(:metadata_json, metadata_json),
        rotated_at = CASE WHEN :ciphertext IS NOT NULL THEN :rotated_at ELSE rotated_at END,
        updated_at = :updated_at
    WHERE credential_id = :credential_id
""")

_DELETE_CREDENTIAL = text("""
    DELETE FROM credential_vault
    WHERE credential_id = :credential_id
""")

_UPDATE_LAST_USED = text("""
    UPDATE credential_vault
    SET last_used_at = :last_used_at,
        updated_at = :updated_at
    WHERE credential_id = :credential_id
""")

# ═══════════════════════════════════════════════════════════════════════════════
# SQL templates — vault_access_logs
# ═══════════════════════════════════════════════════════════════════════════════

_COUNT_ACCESS_LOGS = text("""
    SELECT count(*) AS total
    FROM vault_access_logs
    WHERE credential_id = :credential_id
      AND (:action IS NULL OR action = :action)
      AND (:result IS NULL OR result = :result)
      AND (:occurred_after IS NULL OR occurred_at >= :occurred_after)
      AND (:occurred_before IS NULL OR occurred_at <= :occurred_before)
""")

_QUERY_ACCESS_LOGS = text("""
    SELECT
        access_log_id,
        credential_id,
        actor_type,
        actor_id,
        auth_context_type,
        auth_context_id,
        action,
        result,
        capability_id,
        provider_id,
        request_id,
        correlation_id,
        reason_code,
        target_scope,
        metadata_json,
        occurred_at
    FROM vault_access_logs
    WHERE credential_id = :credential_id
      AND (:action IS NULL OR action = :action)
      AND (:result IS NULL OR result = :result)
      AND (:occurred_after IS NULL OR occurred_at >= :occurred_after)
      AND (:occurred_before IS NULL OR occurred_at <= :occurred_before)
    ORDER BY occurred_at DESC
    LIMIT :limit OFFSET :offset
""")


# ═══════════════════════════════════════════════════════════════════════════════
# Credential CRUD
# ═══════════════════════════════════════════════════════════════════════════════


def get_credentials(
    *,
    page: int = 1,
    page_size: int = 50,
    provider_id: UUID | None = None,
    credential_type: str | None = None,
    status: str | None = None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
) -> tuple[list[dict], int]:
    """Return a page of credentials with the given filters.

    The returned rows do **not** include ``ciphertext`` or ``key_wrap``.
    """
    params = {
        "provider_id": provider_id,
        "credential_type": credential_type,
        "status": status,
        "created_after": created_after,
        "created_before": created_before,
        "limit": page_size,
        "offset": (page - 1) * page_size,
    }

    with SessionLocal() as db:
        total = db.execute(_COUNT_CREDENTIALS, params).scalar_one()
        rows = db.execute(_QUERY_CREDENTIALS, params).mappings().all()
        items = [_credential_row_to_dict(row) for row in rows]
        return items, total


def get_credential_by_id(credential_id: UUID) -> dict | None:
    """Return a single credential row by primary key, or ``None``.

    **Includes** ``ciphertext`` and ``key_wrap`` — callers must NEVER
    expose these fields in API responses.
    """
    with SessionLocal() as db:
        row = (
            db.execute(
                _GET_CREDENTIAL_BY_ID,
                {"credential_id": credential_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        return _credential_row_to_dict(row, include_secrets=True)


def create_credential(
    *,
    provider_id: UUID,
    credential_name: str,
    credential_type: str,
    status: str = "active",
    ciphertext: bytes,
    key_wrap: bytes,
    key_version: str,
    fingerprint: str,
    scope_json: dict | None = None,
    metadata_json: dict | None = None,
    created_by_user_id: UUID | None = None,
) -> dict:
    """Insert a new credential and return the created row as a dict."""
    new_id = uuid4()
    with SessionLocal() as db:
        new_id_raw = db.execute(
            _INSERT_CREDENTIAL,
            {
                "credential_id": new_id,
                "provider_id": provider_id,
                "credential_name": credential_name,
                "credential_type": credential_type,
                "status": status,
                "ciphertext": ciphertext,
                "key_wrap": key_wrap,
                "key_version": key_version,
                "fingerprint": fingerprint,
                "scope_json": _json_dumps(scope_json),
                "metadata_json": _json_dumps(metadata_json),
                "created_by_user_id": created_by_user_id,
            },
        ).scalar_one()
        db.commit()

    return get_credential_by_id(_as_uuid(new_id_raw))


def update_credential(
    *,
    credential_id: UUID,
    status: str | None = None,
    scope_json: dict | None = None,
    metadata_json: dict | None = None,
) -> bool:
    """Update credential metadata (status, scope, metadata) without rotation.

    Returns ``True`` if a row was updated.
    """
    with SessionLocal() as db:
        result = db.execute(
            _UPDATE_CREDENTIAL,
            {
                "credential_id": credential_id,
                "status": status,
                "scope_json": _json_dumps(scope_json),
                "metadata_json": _json_dumps(metadata_json),
                "updated_at": datetime.now(timezone.utc),
            },
        )
        db.commit()
        return result.rowcount > 0


def rotate_credential(
    *,
    credential_id: UUID,
    ciphertext: bytes,
    key_wrap: bytes,
    key_version: str,
    fingerprint: str,
    status: str | None = None,
    scope_json: dict | None = None,
    metadata_json: dict | None = None,
) -> bool:
    """Update credential ciphertext (rotation) with a fresh DEK.

    Returns ``True`` if a row was updated.
    """
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        result = db.execute(
            _UPDATE_CREDENTIAL_WITH_ROTATION,
            {
                "credential_id": credential_id,
                "ciphertext": ciphertext,
                "key_wrap": key_wrap,
                "key_version": key_version,
                "fingerprint": fingerprint,
                "status": status,
                "scope_json": _json_dumps(scope_json),
                "metadata_json": _json_dumps(metadata_json),
                "rotated_at": now,
                "updated_at": now,
            },
        )
        db.commit()
        return result.rowcount > 0


def delete_credential(credential_id: UUID) -> bool:
    """Delete a credential row.

    Returns ``True`` if a row was deleted.
    """
    with SessionLocal() as db:
        result = db.execute(
            _DELETE_CREDENTIAL,
            {"credential_id": credential_id},
        )
        db.commit()
        return result.rowcount > 0


def mark_credential_used(credential_id: UUID) -> None:
    """Update ``last_used_at`` on a credential."""
    try:
        now = datetime.now(timezone.utc)
        with SessionLocal() as db:
            db.execute(
                _UPDATE_LAST_USED,
                {
                    "credential_id": credential_id,
                    "last_used_at": now,
                    "updated_at": now,
                },
            )
            db.commit()
    except Exception:
        logger.warning(
            "Failed to update last_used_at for credential %s", credential_id
        )


# ═══════════════════════════════════════════════════════════════════════════════
# P2-10: Credential lookup via capability_bindings
# ═══════════════════════════════════════════════════════════════════════════════

_GET_CREDENTIAL_ID_BY_BINDING = text("""
    SELECT credential_id
    FROM capability_bindings
    WHERE capability_binding_id = :binding_id
""")


_GET_ACTIVE_CREDENTIAL_FOR_BINDING = text("""
    SELECT
        cv.credential_id,
        cv.provider_id,
        cv.credential_name,
        cv.credential_type,
        cv.status,
        cv.ciphertext,
        cv.key_wrap,
        cv.key_version,
        cv.fingerprint,
        cv.scope_json,
        cv.metadata_json,
        cv.rotated_at,
        cv.last_used_at,
        cv.revoked_at,
        cv.created_by_user_id,
        cv.created_at,
        cv.updated_at
    FROM credential_vault cv
    INNER JOIN capability_bindings cb ON cv.credential_id = cb.credential_id
    WHERE cb.capability_binding_id = :binding_id
      AND cv.status = 'active'
      AND cb.status = 'active'
""")


def get_credential_id_from_binding(binding_id: UUID) -> UUID | None:
    """Return the ``credential_id`` associated with a capability binding.

    Returns ``None`` if the binding does not exist or has no credential.
    """
    with SessionLocal() as db:
        row = db.execute(
            _GET_CREDENTIAL_ID_BY_BINDING,
            {"binding_id": binding_id},
        ).mappings().first()
        if row is None or row["credential_id"] is None:
            return None
        return UUID(str(row["credential_id"]))


def get_active_credential_for_binding(binding_id: UUID) -> dict | None:
    """Return the active credential row for a capability binding.

    Returns ``None`` if the binding does not exist, has no credential,
    or the credential/binding is not active.

    **Includes** ``ciphertext`` and ``key_wrap`` — callers must NEVER
    expose these fields in API responses.
    """
    with SessionLocal() as db:
        row = db.execute(
            _GET_ACTIVE_CREDENTIAL_FOR_BINDING,
            {"binding_id": binding_id},
        ).mappings().first()
        if row is None:
            return None
        return _credential_row_to_dict(row, include_secrets=True)


# ═══════════════════════════════════════════════════════════════════════════════
# Access log queries
# ═══════════════════════════════════════════════════════════════════════════════


def get_vault_access_logs(
    *,
    credential_id: UUID,
    page: int = 1,
    page_size: int = 50,
    action: str | None = None,
    result: str | None = None,
    occurred_after: datetime | None = None,
    occurred_before: datetime | None = None,
) -> tuple[list[dict], int]:
    """Return a page of vault access logs for a credential."""
    params = {
        "credential_id": credential_id,
        "action": action,
        "result": result,
        "occurred_after": occurred_after,
        "occurred_before": occurred_before,
        "limit": page_size,
        "offset": (page - 1) * page_size,
    }

    with SessionLocal() as db:
        total = db.execute(_COUNT_ACCESS_LOGS, params).scalar_one()
        rows = db.execute(_QUERY_ACCESS_LOGS, params).mappings().all()
        items = [_access_log_row_to_dict(row) for row in rows]
        return items, total


# ═══════════════════════════════════════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _json_dumps(obj: dict | None) -> str | None:
    """Serialize a dict to a JSON string for storage.

    Returns ``None`` for ``None`` input (the SQL ``COALESCE`` in UPDATE
    templates treats ``NULL`` as "keep current value").
    """
    if obj is None:
        return None
    return json.dumps(obj, ensure_ascii=False)


def _json_parse(value: object) -> dict:
    """Parse a value into a dict, handling both PostgreSQL JSONB (returns
    ``dict``) and SQLite TEXT (returns ``str`` containing JSON)."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def _as_uuid(value: object) -> UUID:
    """Coerce a value to :class:`uuid.UUID`.

    PostgreSQL's psycopg2 driver returns Python ``UUID`` objects natively.
    SQLite returns hex strings (without dashes) when the column type is
    declared as ``UUID``, or dashed strings when stored as ``TEXT``.
    """
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


def _credential_row_to_dict(row, *, include_secrets: bool = False) -> dict:
    """Convert a SQLAlchemy RowMapping to a plain dict safe for JSON."""

    scope = _json_parse(row.get("scope_json"))
    meta = _json_parse(row.get("metadata_json"))

    result = {
        "credential_id": str(row["credential_id"]),
        "provider_id": str(row["provider_id"]),
        "credential_name": row["credential_name"],
        "credential_type": row["credential_type"],
        "status": row["status"],
        "key_version": row["key_version"],
        "fingerprint": row["fingerprint"],
        "scope_json": scope,
        "metadata_json": meta,
        "rotated_at": _isoformat(row.get("rotated_at")),
        "last_used_at": _isoformat(row.get("last_used_at")),
        "revoked_at": _isoformat(row.get("revoked_at")),
        "created_by_user_id": str(row["created_by_user_id"])
        if row.get("created_by_user_id") is not None
        else None,
        "created_at": _isoformat(row.get("created_at")),
        "updated_at": _isoformat(row.get("updated_at")),
    }

    if include_secrets:
        result["ciphertext"] = row.get("ciphertext")
        result["key_wrap"] = row.get("key_wrap")

    return result


def _access_log_row_to_dict(row) -> dict:
    """Convert a vault_access_logs RowMapping to a plain dict."""

    target_scope = _json_parse(row.get("target_scope"))
    meta = _json_parse(row.get("metadata_json"))

    return {
        "access_log_id": str(row["access_log_id"]),
        "credential_id": str(row["credential_id"])
        if row.get("credential_id") is not None
        else None,
        "actor_type": row["actor_type"],
        "actor_id": str(row["actor_id"])
        if row.get("actor_id") is not None
        else None,
        "auth_context_type": row.get("auth_context_type"),
        "auth_context_id": str(row["auth_context_id"])
        if row.get("auth_context_id") is not None
        else None,
        "action": row["action"],
        "result": row["result"],
        "capability_id": str(row["capability_id"])
        if row.get("capability_id") is not None
        else None,
        "provider_id": str(row["provider_id"])
        if row.get("provider_id") is not None
        else None,
        "request_id": str(row["request_id"])
        if row.get("request_id") is not None
        else None,
        "correlation_id": str(row["correlation_id"])
        if row.get("correlation_id") is not None
        else None,
        "reason_code": row.get("reason_code"),
        "target_scope": target_scope,
        "metadata_json": meta,
        "occurred_at": _isoformat(row.get("occurred_at")),
    }


def _isoformat(dt: datetime | str | None) -> str | None:
    """Return ISO-8601 string for *dt*, or ``None``.

    Handles both Python ``datetime`` objects (PostgreSQL) and ISO strings
    (SQLite stores timestamps as ``TEXT``).
    """
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    return dt.isoformat()
