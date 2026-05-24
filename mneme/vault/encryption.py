"""P2-08 Envelope encryption for Vault credentials.

Envelope encryption scheme
--------------------------
Each credential is encrypted with a two-tier key hierarchy:

1. **DEK** (Data Encryption Key) — a fresh 256-bit random key generated
   per credential.  The plaintext credential is encrypted with AES-256-GCM
   using this DEK.  The result (nonce ‖ ciphertext ‖ tag) is stored in
   ``credential_vault.ciphertext``.

2. **KEK** (Key Encryption Key) — a long-lived 256-bit key configured via
   ``MNEME_VAULT_KEK``.  The DEK is *wrapped* with AES-256-GCM using the
   KEK.  The wrapped DEK (nonce ‖ encrypted_dek ‖ tag) is stored in
   ``credential_vault.key_wrap``.

3. **key_version** — an opaque string (e.g. ``"v1"``) stored alongside
   each credential that identifies which KEK was used.  This enables
   seamless key rotation: old credentials can still be decrypted as long
   as the corresponding KEK is available.

Fingerprint
-----------
``credential_vault.fingerprint`` is a SHA-256 hex digest of the raw
plaintext.  It allows detecting whether a credential's value has changed
without revealing the plaintext.

Security properties
-------------------
- Plaintext never appears in logs, API responses (except reveal), or
  error messages.
- Each credential gets a unique DEK, so compromising one DEK does not
  expose other credentials.
- The KEK is the only long-lived secret; it should be stored in a secure
  secrets manager in production.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from functools import lru_cache
from typing import NamedTuple

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from mneme.config import get_settings

logger = logging.getLogger(__name__)

# AES-GCM nonce size (96 bits = 12 bytes) — standard for GCM
_NONCE_SIZE = 12
# DEK size (256 bits)
_DEK_SIZE = 32


class _EncryptedData(NamedTuple):
    """Result of encrypting data with AES-256-GCM.

    The raw bytes format is: ``nonce (12 bytes) ‖ ciphertext (variable) ‖ tag (16 bytes)``.
    """

    nonce: bytes
    ciphertext: bytes
    tag: bytes

    @classmethod
    def from_combined(cls, blob: bytes) -> "_EncryptedData":
        """Parse a combined blob back into components."""
        if len(blob) < _NONCE_SIZE + 16:
            raise ValueError("Encrypted blob too short")
        return cls(
            nonce=blob[:_NONCE_SIZE],
            ciphertext=blob[_NONCE_SIZE:-16],
            tag=blob[-16:],
        )

    def to_combined(self) -> bytes:
        """Serialise to nonce ‖ ciphertext ‖ tag for storage."""
        return self.nonce + self.ciphertext + self.tag


class _DecryptionError(ValueError):
    """Raised when decryption fails (wrong key, tampered data, or corrupted blob)."""


class VaultEncryption:
    """Stateless envelope encryption service for credential vault.

    Usage::

        vault = VaultEncryption(kek=b"...", key_version="v1")

        # Encrypt
        ciphertext, key_wrap, fingerprint = vault.encrypt(b"my-api-key")

        # Decrypt
        plaintext = vault.decrypt(ciphertext, key_wrap)

        # Re-encrypt with a new DEK (rotation)
        new_ciphertext, new_key_wrap, _ = vault.encrypt(plaintext)
    """

    def __init__(self, *, kek: bytes, key_version: str) -> None:
        if len(kek) != _DEK_SIZE:
            raise ValueError(
                f"KEK must be exactly {_DEK_SIZE} bytes (256 bits), "
                f"got {len(kek)} bytes"
            )
        self._kek = kek
        self._key_version = key_version
        self._aesgcm = AESGCM(kek)

    @property
    def key_version(self) -> str:
        return self._key_version

    # ── Public API ───────────────────────────────────────────────────────────

    def encrypt(self, plaintext: bytes) -> tuple[bytes, bytes, str]:
        """Encrypt *plaintext* and return ``(ciphertext, key_wrap, fingerprint)``.

        Parameters
        ----------
        plaintext : bytes
            The raw credential value to encrypt.

        Returns
        -------
        tuple[bytes, bytes, str]
            - ``ciphertext`` — DEK-encrypted plaintext (stored in
              ``credential_vault.ciphertext``).
            - ``key_wrap`` — KEK-wrapped DEK (stored in
              ``credential_vault.key_wrap``).
            - ``fingerprint`` — SHA-256 hex digest of *plaintext*.
        """
        if not plaintext:
            raise ValueError("plaintext must not be empty")

        # 1. Generate a fresh DEK
        dek = os.urandom(_DEK_SIZE)

        # 2. Encrypt plaintext with DEK
        encrypted_plaintext = self._encrypt_with_key(
            key=dek,
            plaintext=plaintext,
        )

        # 3. Wrap DEK with KEK
        wrapped_dek = self._encrypt_with_key(
            key=self._kek,
            plaintext=dek,
        )

        # 4. Compute fingerprint
        fingerprint = hashlib.sha256(plaintext).hexdigest()

        return (
            encrypted_plaintext.to_combined(),
            wrapped_dek.to_combined(),
            fingerprint,
        )

    def decrypt(self, ciphertext: bytes, key_wrap: bytes) -> bytes:
        """Decrypt *ciphertext* using the wrapped DEK in *key_wrap*.

        Parameters
        ----------
        ciphertext : bytes
            Combined blob (nonce ‖ ciphertext ‖ tag) from ``credential_vault.ciphertext``.
        key_wrap : bytes
            Combined blob (nonce ‖ encrypted_dek ‖ tag) from ``credential_vault.key_wrap``.

        Returns
        -------
        bytes
            The original plaintext credential value.

        Raises
        ------
        _DecryptionError
            If decryption fails (wrong KEK, tampered data, or corrupted storage).
        """
        try:
            # Ensure bytes (PostgreSQL returns memoryview)
            if not isinstance(ciphertext, bytes):
                ciphertext = bytes(ciphertext)
            if not isinstance(key_wrap, bytes):
                key_wrap = bytes(key_wrap)

            # 1. Unwrap DEK with KEK
            wrapped = _EncryptedData.from_combined(key_wrap)
            dek = self._decrypt_with_key(key=self._kek, encrypted=wrapped)

            if len(dek) != _DEK_SIZE:
                raise _DecryptionError("Unwrapped DEK has wrong length")

            # 2. Decrypt ciphertext with DEK
            encrypted = _EncryptedData.from_combined(ciphertext)
            plaintext = self._decrypt_with_key(key=dek, encrypted=encrypted)

            return plaintext
        except (ValueError, _DecryptionError) as exc:
            logger.warning("Vault decryption failed: %s", exc)
            raise _DecryptionError(
                "Credential decryption failed — the KEK may have been rotated "
                "or the stored data is corrupted"
            ) from exc

    def verify_fingerprint(self, plaintext: bytes, fingerprint: str) -> bool:
        """Check whether *plaintext* matches the stored *fingerprint*."""
        expected = hashlib.sha256(plaintext).hexdigest()
        return expected == fingerprint

    # ── Internal helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _encrypt_with_key(*, key: bytes, plaintext: bytes) -> _EncryptedData:
        """Encrypt *plaintext* with AES-256-GCM using *key*.

        Returns an :class:`_EncryptedData` with nonce, ciphertext, and tag.
        """
        nonce = os.urandom(_NONCE_SIZE)
        aesgcm = AESGCM(key)
        # AESGCM.encrypt returns ciphertext ‖ tag (tag is appended automatically)
        ct_with_tag = aesgcm.encrypt(nonce, plaintext, associated_data=None)
        # The tag is the last 16 bytes
        ct = ct_with_tag[:-16]
        tag = ct_with_tag[-16:]
        return _EncryptedData(nonce=nonce, ciphertext=ct, tag=tag)

    @staticmethod
    def _decrypt_with_key(*, key: bytes, encrypted: _EncryptedData) -> bytes:
        """Decrypt *encrypted* with AES-256-GCM using *key*.

        Raises :class:`_DecryptionError` on authentication failure.
        """
        aesgcm = AESGCM(key)
        combined = encrypted.ciphertext + encrypted.tag
        try:
            return aesgcm.decrypt(
                encrypted.nonce, combined, associated_data=None
            )
        except Exception as exc:
            raise _DecryptionError(str(exc)) from exc


# ═══════════════════════════════════════════════════════════════════════════════
# Singleton factory
# ═══════════════════════════════════════════════════════════════════════════════


@lru_cache
def _get_vault_encryption() -> VaultEncryption:
    """Return the singleton :class:`VaultEncryption` instance.

    The KEK is loaded from ``MNEME_VAULT_KEK``.  If the env var is empty or
    unset, a **random** KEK is generated.  This is safe for development but
    **must be replaced with a persistent secret in production**, otherwise
    all encrypted credentials become irrecoverable after a restart.
    """
    settings = get_settings()
    kek_b64 = settings.vault_kek

    if kek_b64:
        try:
            kek = base64.b64decode(kek_b64)
        except Exception as exc:
            raise ValueError(
                "MNEME_VAULT_KEK must be a valid base64-encoded 256-bit key"
            ) from exc
    else:
        # Development fallback — generate a random KEK
        kek = os.urandom(_DEK_SIZE)
        logger.warning(
            "MNEME_VAULT_KEK is not set — generated a random KEK. "
            "Encrypted credentials WILL BE LOST after restart! "
            "Set MNEME_VAULT_KEK in production."
        )

    if len(kek) != _DEK_SIZE:
        raise ValueError(
            f"KEK must be exactly {_DEK_SIZE} bytes (256 bits), "
            f"got {len(kek)} bytes after decoding"
        )

    return VaultEncryption(kek=kek, key_version=settings.vault_key_version)


def get_vault_encryption() -> VaultEncryption:
    """Return the configured :class:`VaultEncryption` singleton."""
    return _get_vault_encryption()
