"""P2-09 Vault access log helpers.

Provides a :func:`write_vault_access_log` function that writes a row to
``vault_access_logs``.  This is called by the Vault API routes on every
credential operation (create, read, reveal, update, delete, etc.).

**Database compatibility**: The INSERT template avoids dialect-specific
types (no ``PG_UUID``, ``JSONB``).  UUIDs are passed as Python ``UUID``
objects and JSON columns as plain ``dict`` — SQLAlchemy handles the
conversion for both PostgreSQL and SQLite.
"""

from __future__ import annotations

import json
import logging
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text

from mneme.db.base import SessionLocal

logger = logging.getLogger(__name__)


class VaultAccessAction(str, Enum):
    """Values for ``vault_access_logs.action`` (DDL CHECK constraint)."""

    create = "create"
    enable = "enable"
    disable = "disable"
    rotate = "rotate"
    revoke = "revoke"
    export = "export"
    use = "use"
    access_denied = "access_denied"


class VaultAccessResult(str, Enum):
    """Values for ``vault_access_logs.result`` (DDL CHECK constraint)."""

    success = "success"
    denied = "denied"
    failed = "failed"


_INSERT_ACCESS_LOG = text("""
    INSERT INTO vault_access_logs (
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
        metadata_json
    ) VALUES (
        :access_log_id,
        :credential_id,
        :actor_type,
        :actor_id,
        :auth_context_type,
        :auth_context_id,
        :action,
        :result,
        :capability_id,
        :provider_id,
        :request_id,
        :correlation_id,
        :reason_code,
        :target_scope,
        :metadata_json
    )
    RETURNING access_log_id
""")


def write_vault_access_log(
    *,
    credential_id: UUID | None = None,
    actor_type: str = "system",
    actor_id: UUID | None = None,
    auth_context_type: str | None = None,
    auth_context_id: UUID | None = None,
    action: str,
    result: str,
    capability_id: UUID | None = None,
    provider_id: UUID | None = None,
    request_id: UUID | None = None,
    correlation_id: UUID | None = None,
    reason_code: str | None = None,
    target_scope: dict[str, Any] | None = None,
    metadata_json: dict[str, Any] | None = None,
) -> UUID:
    """Write a row to ``vault_access_logs``.

    This function is **fire-and-forget** for logging purposes.  It opens its
    own short-lived session and commits immediately.  Failures are logged but
    do not propagate to the caller — access logging is best-effort.

    All UUID parameters are passed as Python :class:`~uuid.UUID` objects;
    JSON columns (``target_scope``, ``metadata_json``) are serialised via
    :func:`json.dumps`.  This ensures compatibility with both PostgreSQL
    and SQLite backends.

    Parameters
    ----------
    credential_id : UUID | None
        The credential being accessed (nullable for operations like listing
        where no single credential is targeted).
    actor_type : str
        One of ``user``, ``agent``, ``service``, ``system``.
    actor_id : UUID | None
        The authenticated actor's id.
    auth_context_type : str | None
        One of ``user_session``, ``agent_token``, ``service_identity``, ``system_job``.
    auth_context_id : UUID | None
        The auth context's id.
    action : str
        One of :class:`VaultAccessAction` values.
    result : str
        One of :class:`VaultAccessResult` values.
    capability_id : UUID | None
        The capability id if the access was through a capability binding.
    provider_id : UUID | None
        The provider id associated with the credential.
    request_id : UUID | None
        The API request id.
    correlation_id : UUID | None
        The correlation id for tracing.
    reason_code : str | None
        Reason for denial or failure.
    target_scope : dict | None
        JSON describing the scope of the access.
    metadata_json : dict | None
        Additional metadata (never contains plaintext credentials).

    Returns
    -------
    UUID
        The newly created ``access_log_id``.
    """
    access_log_id = uuid4()
    try:
        with SessionLocal() as db:
            db.execute(
                _INSERT_ACCESS_LOG,
                {
                    "access_log_id": access_log_id,
                    "credential_id": credential_id,
                    "actor_type": actor_type,
                    "actor_id": actor_id,
                    "auth_context_type": auth_context_type,
                    "auth_context_id": auth_context_id,
                    "action": action,
                    "result": result,
                    "capability_id": capability_id,
                    "provider_id": provider_id,
                    "request_id": request_id,
                    "correlation_id": correlation_id,
                    "reason_code": reason_code,
                    "target_scope": json.dumps(target_scope) if target_scope else "{}",
                    "metadata_json": json.dumps(metadata_json) if metadata_json else "{}",
                },
            )
            db.commit()
        return access_log_id
    except Exception:
        logger.exception(
            "Failed to write vault access log (action=%s, result=%s, credential=%s)",
            action,
            result,
            credential_id,
        )
        # Best-effort — do not propagate; return the pre-generated id so
        # callers can still link back to this log entry if it was persisted.
        return access_log_id
