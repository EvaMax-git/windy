"""Encrypt facade — unified encryption interface.

Provides simple top-level functions for file encryption:
    encrypt_file  → encrypt content, return (encrypted_data, key)
    decrypt_file  → decrypt content with key
    generate_key  → generate a new 256-bit AES key
"""

from __future__ import annotations

from mneme.security.file_encrypt import (
    decrypt_file as _decrypt_file,
    encrypt_file as _encrypt_file,
    generate_key as _generate_key,
)


def encrypt_file(content: bytes, key: bytes | None = None) -> tuple[bytes, bytes]:
    """Encrypt file content with AES-256-GCM.

    Args:
        content: Raw file bytes to encrypt.
        key: 32-byte AES key. If None, a new key is generated.

    Returns:
        Tuple of (encrypted_data, key).

    Raises:
        ValueError: If content is empty or key is wrong size.
        TypeError: If content is not bytes.
    """
    if not isinstance(content, bytes):
        raise TypeError("content must be bytes")
    if not content:
        raise ValueError("content must not be empty")
    if key is None:
        key = _generate_key()
    # _encrypt_file returns (encrypted_bytes, key) tuple
    encrypted, _ = _encrypt_file(content, key)
    return encrypted, key


def decrypt_file(encrypted: bytes, key: bytes) -> bytes:
    """Decrypt AES-256-GCM encrypted content.

    Args:
        encrypted: Encrypted bytes (with or without MNME magic header).
        key: 32-byte AES key used for encryption.

    Returns:
        Original plaintext bytes.

    Raises:
        ValueError: If key is wrong or data is corrupted.
        TypeError: If encrypted or key is not bytes.
    """
    if not isinstance(encrypted, bytes):
        raise TypeError("encrypted must be bytes")
    if not isinstance(key, bytes):
        raise TypeError("key must be bytes")
    return _decrypt_file(encrypted, key)


def generate_key() -> bytes:
    """Generate a new 256-bit AES key.

    Returns:
        32 random bytes suitable for AES-256-GCM.
    """
    return _generate_key()
