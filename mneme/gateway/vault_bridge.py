"""P2-10 Vault-Gateway integration — credential resolution for Gateway calls.

This module provides the :class:`VaultCredentialResolver` that Gateway uses to
automatically look up and decrypt Vault credentials when making external API
calls.

Architecture
------------
1. Gateway resolves a ``capability_binding`` to find the target provider, model,
   and the associated ``credential_id``.
2. The resolver looks up the credential in ``credential_vault``, checks its
   status, and decrypts the stored ciphertext.
3. Every resolution attempt (success or denial) is recorded in
   ``vault_access_logs``.
4. If the credential is missing, revoked, disabled, or cannot be decrypted,
   the resolver raises :class:`CredentialNotAvailable` and the Gateway call
   is rejected.

Usage::

    from mneme.gateway.vault_bridge import get_vault_credential_resolver

    resolver = get_vault_credential_resolver()

    try:
        plaintext = resolver.resolve(
            credential_id=uuid4(),
            capability_id=uuid4(),
            provider_id=uuid4(),
            request_id=uuid4(),
            correlation_id=uuid4(),
        )
    except CredentialNotAvailable as exc:
        # Gateway rejects the call
        raise ApiError(403, "permission_denied", str(exc))
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from mneme.api.context import ActorContext
from mneme.db.base import SessionLocal
from mneme.db.vault import get_credential_by_id
from mneme.vault.access_log import (
    VaultAccessAction,
    VaultAccessResult,
    write_vault_access_log,
)
from mneme.vault.encryption import get_vault_encryption, _DecryptionError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedCredential:
    """Result of a successful Vault credential resolution.

    Attributes
    ----------
    plaintext : bytes
        The decrypted credential value.
    vault_access_log_id : UUID
        The ``vault_access_logs.access_log_id`` for this resolution,
        for linking to ``api_call_logs.vault_access_log_id``.
    """
    plaintext: bytes
    vault_access_log_id: UUID


class CredentialNotAvailable(Exception):
    """Raised when a Vault credential cannot be resolved for a Gateway call.

    This is a terminal error for the Gateway call — the request is rejected
    and the denial is recorded in ``vault_access_logs`` with an appropriate
    ``reason_code``.
    """

    def __init__(
        self,
        credential_id: UUID | None,
        reason_code: str,
        message: str,
    ) -> None:
        super().__init__(message)
        self.credential_id = credential_id
        self.reason_code = reason_code


class VaultCredentialResolver:
    """Resolve (lookup + decrypt) Vault credentials for Gateway calls.

    This is a stateless service.  Every call to :meth:`resolve` opens its own
    database session and writes a vault access log entry.

    Thread-safe for concurrent use.
    """

    # ── Public API ───────────────────────────────────────────────────────────

    def resolve(
        self,
        *,
        credential_id: UUID,
        capability_id: UUID | None = None,
        provider_id: UUID | None = None,
        request_id: UUID | None = None,
        correlation_id: UUID | None = None,
        actor_type: str = "system",
        actor_id: UUID | None = None,
    ) -> ResolvedCredential:
        """Look up and decrypt a Vault credential.

        Parameters
        ----------
        credential_id : UUID
            The ``credential_vault.credential_id`` to resolve.
        capability_id : UUID | None
            The ``capability_id`` from the capability binding, for access logging.
        provider_id : UUID | None
            The ``provider_id`` associated with the credential, for access logging.
        request_id : UUID | None
            The API request id for trace correlation.
        correlation_id : UUID | None
            The correlation id for trace correlation.
        actor_type : str
            The actor type making the call (``"system"``, ``"agent"``, etc.).
        actor_id : UUID | None
            The actor id.

        Returns
        -------
        ResolvedCredential
            The decrypted plaintext credential value and the vault_access_log_id.

        Raises
        ------
        CredentialNotAvailable
            If the credential does not exist, is not active, or cannot be decrypted.
        """
        # 1. Look up the credential
        row = get_credential_by_id(credential_id)
        if row is None:
            self._log_denial(
                credential_id=credential_id,
                capability_id=capability_id,
                provider_id=provider_id,
                request_id=request_id,
                correlation_id=correlation_id,
                actor_type=actor_type,
                actor_id=actor_id,
                reason_code="credential_not_found",
            )
            raise CredentialNotAvailable(
                credential_id=credential_id,
                reason_code="credential_not_found",
                message=f"Credential '{credential_id}' not found in vault",
            )

        # 2. Check status — only 'active' credentials are usable via Gateway
        status = row["status"]
        if status != "active":
            self._log_denial(
                credential_id=credential_id,
                capability_id=capability_id,
                provider_id=UUID(row["provider_id"]),
                request_id=request_id,
                correlation_id=correlation_id,
                actor_type=actor_type,
                actor_id=actor_id,
                reason_code=f"credential_{status}",
            )
            raise CredentialNotAvailable(
                credential_id=credential_id,
                reason_code=f"credential_{status}",
                message=f"Credential '{credential_id}' is {status} (must be active)",
            )

        # 3. Decrypt
        vault = get_vault_encryption()
        try:
            ciphertext = row["ciphertext"]
            key_wrap = row["key_wrap"]

            if not isinstance(ciphertext, bytes):
                ciphertext = bytes(ciphertext)
            if not isinstance(key_wrap, bytes):
                key_wrap = bytes(key_wrap)

            plaintext = vault.decrypt(ciphertext, key_wrap)
        except _DecryptionError as exc:
            self._log_denial(
                credential_id=credential_id,
                capability_id=capability_id,
                provider_id=UUID(row["provider_id"]),
                request_id=request_id,
                correlation_id=correlation_id,
                actor_type=actor_type,
                actor_id=actor_id,
                reason_code="decryption_failed",
                metadata_json={"error": str(exc)},
            )
            raise CredentialNotAvailable(
                credential_id=credential_id,
                reason_code="decryption_failed",
                message="Credential decryption failed — the KEK may have been rotated",
            )

        # 4. Verify fingerprint
        if not vault.verify_fingerprint(plaintext, row["fingerprint"]):
            self._log_denial(
                credential_id=credential_id,
                capability_id=capability_id,
                provider_id=UUID(row["provider_id"]),
                request_id=request_id,
                correlation_id=correlation_id,
                actor_type=actor_type,
                actor_id=actor_id,
                reason_code="fingerprint_mismatch",
            )
            raise CredentialNotAvailable(
                credential_id=credential_id,
                reason_code="fingerprint_mismatch",
                message="Credential integrity check failed — fingerprint mismatch",
            )

        # 5. Log successful access
        vault_access_log_id = write_vault_access_log(
            credential_id=credential_id,
            actor_type=actor_type,
            actor_id=actor_id,
            action=VaultAccessAction.use.value,
            result=VaultAccessResult.success.value,
            capability_id=capability_id,
            provider_id=UUID(row["provider_id"]),
            request_id=request_id,
            correlation_id=correlation_id,
        )

        logger.info(
            "Vault credential resolved for Gateway: credential_id=%s capability=%s",
            credential_id,
            capability_id,
        )

        return ResolvedCredential(
            plaintext=plaintext,
            vault_access_log_id=vault_access_log_id,
        )

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _log_denial(
        self,
        *,
        credential_id: UUID,
        capability_id: UUID | None = None,
        provider_id: UUID | None = None,
        request_id: UUID | None = None,
        correlation_id: UUID | None = None,
        actor_type: str = "system",
        actor_id: UUID | None = None,
        reason_code: str,
        metadata_json: dict[str, Any] | None = None,
    ) -> None:
        """Write a denial entry to ``vault_access_logs``."""
        write_vault_access_log(
            credential_id=credential_id,
            actor_type=actor_type,
            actor_id=actor_id,
            action=VaultAccessAction.access_denied.value,
            result=VaultAccessResult.denied.value,
            capability_id=capability_id,
            provider_id=provider_id,
            request_id=request_id,
            correlation_id=correlation_id,
            reason_code=reason_code,
            metadata_json=metadata_json,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Singleton factory
# ═══════════════════════════════════════════════════════════════════════════════

_resolver: VaultCredentialResolver | None = None


def get_vault_credential_resolver() -> VaultCredentialResolver:
    """Return the module-level :class:`VaultCredentialResolver` singleton."""
    global _resolver
    if _resolver is None:
        _resolver = VaultCredentialResolver()
    return _resolver
