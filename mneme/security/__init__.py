"""Mneme security module.

Contains:
* Password hashing and session/agent token generation utilities.
* Policy Engine – ``can(actor, action, object, context) -> PolicyDecision``.
* Review Router – auto-create ``review_items`` from policy decisions.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from mneme.security.policy import (
    Action,
    Actor,
    Decision,
    DenyReason,
    Object,
    PolicyContext,
    PolicyDecision,
    actor_from_agent_token,
    actor_from_user_session,
    actor_system,
    can,
)


PASSWORD_HASH_ALGORITHM = "pbkdf2_sha256"
PASSWORD_HASH_ITERATIONS = 210_000
SESSION_TOKEN_BYTES = 32


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _base64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PASSWORD_HASH_ITERATIONS,
    )
    return "$".join(
        [
            PASSWORD_HASH_ALGORITHM,
            str(PASSWORD_HASH_ITERATIONS),
            _base64url_encode(salt),
            _base64url_encode(digest),
        ]
    )


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, iterations_text, salt_text, expected_text = stored_hash.split("$", 3)
        if algorithm != PASSWORD_HASH_ALGORITHM:
            return False
        iterations = int(iterations_text)
        salt = _base64url_decode(salt_text)
        expected = _base64url_decode(expected_text)
    except (ValueError, TypeError):
        return False

    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def generate_session_token() -> str:
    return secrets.token_urlsafe(SESSION_TOKEN_BYTES)


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_optional_fingerprint(value: str | None) -> str | None:
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


AGENT_TOKEN_BYTES = 48


def generate_agent_token() -> str:
    return secrets.token_urlsafe(AGENT_TOKEN_BYTES)


def hash_agent_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def compute_agent_token_fingerprint(token: str, agent_id: str, issued_by: str) -> str:
    payload = f"{agent_id}:{token[:8]}:{issued_by}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


from mneme.security.audit import (  # noqa: E402  (import after __all__ for namespace)
    audit_event_for_action,
    audit_event_for_auth,
    audit_event_for_policy_denied,
    audit_event_for_policy_review_required,
    audit_event_for_policy_step_up_required,
    outbox_event_for_action,
    emit_audit_event,
    emit_outbox_event,
    emit_audit_and_outbox,
    write_with_audit_and_outbox,
    write_with_audit_outbox_idempotency,
)
from mneme.db.audit import AuditEvent, OutboxEvent  # noqa: E402
from mneme.security.review_router import (  # noqa: E402
    ReviewRouteRule,
    ReviewRoutingEngine,
    get_review_routing_engine,
    handle_review_required,
    determine_review_type,
    does_action_require_review,
)

# Backward-compatible alias
ReviewRouter = ReviewRoutingEngine


__all__ = [
    # ── password / token utilities ──
    "hash_password",
    "verify_password",
    "generate_session_token",
    "hash_session_token",
    "hash_optional_fingerprint",
    "generate_agent_token",
    "hash_agent_token",
    "compute_agent_token_fingerprint",
    # ── Policy Engine ──
    "can",
    "Actor",
    "Action",
    "Object",
    "PolicyContext",
    "PolicyDecision",
    "Decision",
    "DenyReason",
    "actor_from_user_session",
    "actor_from_agent_token",
    "actor_system",
    # ── Review Router (P2-06) ──
    "ReviewRouteRule",
    "ReviewRoutingEngine",
    "ReviewRouter",
    "get_review_routing_engine",
    "handle_review_required",
    "determine_review_type",
    "does_action_require_review",
    # ── Audit event factories ──
    "audit_event_for_action",
    "audit_event_for_auth",
    "audit_event_for_policy_denied",
    "audit_event_for_policy_review_required",
    "audit_event_for_policy_step_up_required",
    # ── Outbox event factories ──
    "outbox_event_for_action",
    # ── Audit / outbox emit helpers (construct + write) ──
    "emit_audit_event",
    "emit_outbox_event",
    "emit_audit_and_outbox",
    # ── Transactional write helpers ──
    "write_with_audit_and_outbox",
    "write_with_audit_outbox_idempotency",
    # ── Dataclasses ──
    "AuditEvent",
    "OutboxEvent",
]
