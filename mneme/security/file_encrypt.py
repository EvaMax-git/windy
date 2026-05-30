"""AES-256-GCM file encryption utilities.

Provides authenticated encryption for file content using AES-256-GCM.
"""

import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE_SIZE = 12  # 96 bits for GCM
_KEY_SIZE = 32    # 256 bits
_MAGIC = b'MNME'  # 4-byte magic header for encrypted files
_MAGIC_SIZE = 4


def generate_key() -> bytes:
    """Generate a 256-bit AES key."""
    return os.urandom(_KEY_SIZE)


def is_encrypted(content: bytes) -> bool:
    """Check if content has MNME magic header (encrypted file marker)."""
    return (
        len(content) >= _MAGIC_SIZE + _NONCE_SIZE + 16
        and content[:_MAGIC_SIZE] == _MAGIC
    )


def encrypt_file(content: bytes, key: bytes = None) -> tuple[bytes, bytes]:
    """AES-256-GCM encrypt.

    Args:
        content: Raw file content to encrypt.
        key: 256-bit AES key. If None, a new key is generated.

    Returns:
        Tuple of (encrypted_data, key).
        encrypted_data format: MNME (4) + nonce (12) + ciphertext + tag (16).

    Raises:
        ValueError: If content is empty or key is wrong size.
    """
    if not content:
        raise ValueError("content must not be empty")

    if key is None:
        key = generate_key()
    elif len(key) != _KEY_SIZE:
        raise ValueError(f"key must be {_KEY_SIZE} bytes")

    nonce = os.urandom(_NONCE_SIZE)
    aesgcm = AESGCM(key)
    ct = aesgcm.encrypt(nonce, content, associated_data=None)
    return (_MAGIC + nonce + ct, key)


def decrypt_file(encrypted: bytes, key: bytes) -> bytes:
    """AES-256-GCM decrypt. Supports both magic-header and legacy formats."""
    if len(key) != _KEY_SIZE:
        raise ValueError(f"key must be {_KEY_SIZE} bytes")
    # Skip magic header if present
    data = encrypted
    if data[:_MAGIC_SIZE] == _MAGIC:
        data = data[_MAGIC_SIZE:]
    if len(data) < _NONCE_SIZE + 16:
        raise ValueError("encrypted data too short")
    nonce = data[:_NONCE_SIZE]
    ct = data[_NONCE_SIZE:]
    aesgcm = AESGCM(key)
    try:
        return aesgcm.decrypt(nonce, ct, associated_data=None)
    except InvalidTag as e:
        raise ValueError(f"解密失败: {e}") from e
