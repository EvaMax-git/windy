"""P2-08 Vault encryption tests — roundtrip, key management, edge cases.

Tests cover:
1. Encrypt/decrypt roundtrip
2. Wrong KEK fails
3. Tampered data fails
4. Unique DEK per encryption
5. Fingerprint verification
6. Empty plaintext rejection
7. Wrong KEK size rejection
8. Key rotation (re-encrypt with fresh DEK)
"""

from __future__ import annotations

import os

import pytest

from mneme.vault.encryption import VaultEncryption, _DecryptionError


@pytest.fixture
def kek() -> bytes:
    return os.urandom(32)


@pytest.fixture
def vault(kek: bytes) -> VaultEncryption:
    return VaultEncryption(kek=kek, key_version="v1")


@pytest.fixture
def plaintext() -> bytes:
    return b"sk-this-is-a-test-api-key-1234567890"


# ── Basic roundtrip ──────────────────────────────────────────────────────────


def test_encrypt_decrypt_roundtrip(vault: VaultEncryption, plaintext: bytes) -> None:
    """Plaintext → encrypt → decrypt → same plaintext."""
    ct, kw, fp = vault.encrypt(plaintext)
    decrypted = vault.decrypt(ct, kw)
    assert decrypted == plaintext


def test_ciphertext_is_not_plaintext(vault: VaultEncryption, plaintext: bytes) -> None:
    """Ciphertext must not contain the plaintext."""
    ct, kw, fp = vault.encrypt(plaintext)
    assert plaintext not in ct
    assert plaintext not in kw


# ── Fingerprint ──────────────────────────────────────────────────────────────


def test_fingerprint_matches(vault: VaultEncryption, plaintext: bytes) -> None:
    """Fingerprint must verify against original plaintext."""
    ct, kw, fp = vault.encrypt(plaintext)
    assert vault.verify_fingerprint(plaintext, fp)


def test_fingerprint_differs(vault: VaultEncryption, plaintext: bytes) -> None:
    """Different plaintext must produce different fingerprint."""
    _, _, fp1 = vault.encrypt(plaintext)
    _, _, fp2 = vault.encrypt(b"different-key")
    assert fp1 != fp2


# ── Uniqueness ───────────────────────────────────────────────────────────────


def test_unique_dek_per_encryption(vault: VaultEncryption, plaintext: bytes) -> None:
    """Each encryption must use a fresh DEK — same plaintext, different ciphertext."""
    ct1, kw1, _ = vault.encrypt(plaintext)
    ct2, kw2, _ = vault.encrypt(plaintext)
    assert ct1 != ct2, "Same plaintext should produce different ciphertext"
    assert kw1 != kw2, "Same plaintext should produce different key_wrap"


def test_different_plaintext_different_ciphertext(vault: VaultEncryption) -> None:
    """Different plaintexts must produce different ciphertexts."""
    ct1, _, _ = vault.encrypt(b"key-a")
    ct2, _, _ = vault.encrypt(b"key-b")
    assert ct1 != ct2


# ── Wrong KEK ────────────────────────────────────────────────────────────────


def test_wrong_kek_fails(vault: VaultEncryption, plaintext: bytes) -> None:
    """Decryption with a different KEK must fail."""
    ct, kw, _ = vault.encrypt(plaintext)

    wrong_kek = os.urandom(32)
    wrong_vault = VaultEncryption(kek=wrong_kek, key_version="v1")

    with pytest.raises(_DecryptionError):
        wrong_vault.decrypt(ct, kw)


# ── Tampered data ────────────────────────────────────────────────────────────


def test_tampered_ciphertext_fails(vault: VaultEncryption, plaintext: bytes) -> None:
    """Modifying the ciphertext must cause decryption failure."""
    ct, kw, _ = vault.encrypt(plaintext)

    # Flip a bit in the ciphertext
    tampered_ct = bytearray(ct)
    tampered_ct[20] ^= 0x01

    with pytest.raises(_DecryptionError):
        vault.decrypt(bytes(tampered_ct), kw)


def test_tampered_key_wrap_fails(vault: VaultEncryption, plaintext: bytes) -> None:
    """Modifying the key_wrap must cause decryption failure."""
    ct, kw, _ = vault.encrypt(plaintext)

    # Truncate key_wrap
    tampered_kw = kw[:10]

    with pytest.raises(_DecryptionError):
        vault.decrypt(ct, tampered_kw)


def test_empty_ciphertext_fails(vault: VaultEncryption) -> None:
    """Empty or truncated ciphertext must fail."""
    with pytest.raises(_DecryptionError):
        vault.decrypt(b"", os.urandom(60))


# ── Edge cases ───────────────────────────────────────────────────────────────


def test_empty_plaintext_rejected(vault: VaultEncryption) -> None:
    """Empty plaintext must be rejected."""
    with pytest.raises(ValueError, match="plaintext must not be empty"):
        vault.encrypt(b"")


def test_wrong_kek_size_rejected() -> None:
    """KEK must be exactly 32 bytes."""
    with pytest.raises(ValueError, match="KEK must be exactly 32 bytes"):
        VaultEncryption(kek=b"short", key_version="v1")


def test_unicode_plaintext(vault: VaultEncryption) -> None:
    """Unicode (UTF-8) plaintext must encrypt/decrypt correctly."""
    plaintext = "sk-密钥-🔑-テスト".encode("utf-8")
    ct, kw, fp = vault.encrypt(plaintext)
    decrypted = vault.decrypt(ct, kw)
    assert decrypted == plaintext


def test_large_plaintext(vault: VaultEncryption) -> None:
    """Large plaintext (10 KB) must encrypt/decrypt correctly."""
    plaintext = os.urandom(10240)  # 10 KB
    ct, kw, fp = vault.encrypt(plaintext)
    decrypted = vault.decrypt(ct, kw)
    assert decrypted == plaintext
    assert vault.verify_fingerprint(decrypted, fp)


def test_key_version_preserved(vault: VaultEncryption) -> None:
    """key_version must be accessible."""
    assert vault.key_version == "v1"


# ── Rotation scenario ────────────────────────────────────────────────────────


def test_rotation_produces_different_ciphertext(
    vault: VaultEncryption, plaintext: bytes
) -> None:
    """Re-encrypting after rotation must produce different ciphertext."""
    ct1, kw1, fp1 = vault.encrypt(plaintext)

    # Simulate rotation: re-encrypt the same plaintext with fresh DEK
    ct2, kw2, fp2 = vault.encrypt(plaintext)

    # Ciphertext and key_wrap differ (fresh DEK)
    assert ct1 != ct2
    assert kw1 != kw2
    # Fingerprint is same (same plaintext)
    assert fp1 == fp2

    # Both can be decrypted
    assert vault.decrypt(ct1, kw1) == plaintext
    assert vault.decrypt(ct2, kw2) == plaintext


def test_rotation_with_new_kek(
    vault: VaultEncryption, plaintext: bytes
) -> None:
    """Old credentials decrypt with old KEK; new credentials use new KEK version."""
    # Encrypt with old KEK
    ct_old, kw_old, fp_old = vault.encrypt(plaintext)

    # Create "new" KEK for rotation
    new_kek = os.urandom(32)
    new_vault = VaultEncryption(kek=new_kek, key_version="v2")

    # Encrypt with new KEK
    ct_new, kw_new, fp_new = new_vault.encrypt(plaintext)

    # Old ciphertext can still be decrypted with old KEK
    assert vault.decrypt(ct_old, kw_old) == plaintext

    # New ciphertext can be decrypted with new KEK
    assert new_vault.decrypt(ct_new, kw_new) == plaintext

    # Different key versions
    assert vault.key_version == "v1"
    assert new_vault.key_version == "v2"
