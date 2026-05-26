"""AES-256-GCM file encryption utilities.

Provides authenticated encryption for file content using AES-256-GCM.
"""

import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE_SIZE = 12  # 96 bits for GCM
_KEY_SIZE = 32    # 256 bits


def generate_key() -> bytes:
    """Generate a 256-bit AES key."""
    return os.urandom(_KEY_SIZE)


def encrypt_file(content: bytes, key: bytes) -> bytes:
    """AES-256-GCM encrypt. Returns nonce (12) + ciphertext + tag (16)."""
    if not content:
        raise ValueError("content must not be empty")
    if len(key) != _KEY_SIZE:
        raise ValueError(f"key must be {_KEY_SIZE} bytes")
    nonce = os.urandom(_NONCE_SIZE)
    aesgcm = AESGCM(key)
    ct = aesgcm.encrypt(nonce, content, associated_data=None)
    return nonce + ct


def decrypt_file(encrypted: bytes, key: bytes) -> bytes:
    """AES-256-GCM decrypt."""
    if len(encrypted) < _NONCE_SIZE + 16:
        raise ValueError("encrypted data too short")
    if len(key) != _KEY_SIZE:
        raise ValueError(f"key must be {_KEY_SIZE} bytes")
    nonce = encrypted[:_NONCE_SIZE]
    ct = encrypted[_NONCE_SIZE:]
    aesgcm = AESGCM(key)
    try:
        return aesgcm.decrypt(nonce, ct, associated_data=None)
    except InvalidTag as e:
        raise ValueError(f"解密失败: {e}") from e
