"""Tests for AES-256-GCM file encryption — A-17~A-19."""

import pytest

from mneme.security.file_encrypt import decrypt_file, encrypt_file, generate_key, is_encrypted


class TestGenerateKey:
    """Tests for generate_key()."""

    def test_key_length(self):
        """Key must be exactly 32 bytes (256 bits)."""
        key = generate_key()
        assert len(key) == 32

    def test_keys_are_random(self):
        """Two generated keys must differ."""
        key1 = generate_key()
        key2 = generate_key()
        assert key1 != key2


class TestEncryptDecrypt:
    """Tests for encrypt_file() and decrypt_file()."""

    def test_roundtrip(self):
        """Encrypt then decrypt must return original content."""
        content = b"Hello, Mneme!"
        key = generate_key()
        encrypted, returned_key = encrypt_file(content, key)
        decrypted = decrypt_file(encrypted, key)
        assert decrypted == content
        assert returned_key == key

    def test_encrypted_is_different(self):
        """Ciphertext must differ from plaintext."""
        content = b"Hello, Mneme!"
        key = generate_key()
        encrypted, _ = encrypt_file(content, key)
        assert encrypted != content

    def test_wrong_key_fails(self):
        """Decrypting with wrong key must raise ValueError."""
        content = b"Hello, Mneme!"
        key = generate_key()
        wrong_key = generate_key()
        encrypted, _ = encrypt_file(content, key)
        with pytest.raises(ValueError, match="解密失败"):
            decrypt_file(encrypted, wrong_key)

    def test_chinese_content(self):
        """Chinese text roundtrip must succeed."""
        content = "你好世界，这是中文测试内容。".encode("utf-8")
        key = generate_key()
        encrypted, _ = encrypt_file(content, key)
        decrypted = decrypt_file(encrypted, key)
        assert decrypted == content

    def test_empty_content_fails(self):
        """Empty bytes must raise ValueError."""
        key = generate_key()
        with pytest.raises(ValueError, match="content must not be empty"):
            encrypt_file(b"", key)

    def test_same_plaintext_different_ciphertext(self):
        """Encrypting same plaintext twice must produce different ciphertext (random nonce)."""
        content = b"Hello, Mneme!"
        key = generate_key()
        enc1, _ = encrypt_file(content, key)
        enc2, _ = encrypt_file(content, key)
        assert enc1 != enc2  # different nonces

    def test_tampered_ciphertext_fails(self):
        """Flipping a bit in ciphertext must cause decryption to fail (GCM integrity)."""
        content = b"Hello, Mneme!"
        key = generate_key()
        encrypted, _ = encrypt_file(content, key)
        # Flip last byte
        tampered = encrypted[:-1] + bytes([encrypted[-1] ^ 0xFF])
        with pytest.raises(ValueError, match="解密失败"):
            decrypt_file(tampered, key)


class TestMagicHeader:
    """A-21: Magic header for encryption detection."""

    def test_encrypted_has_magic(self):
        """Encrypted file starts with MNME magic bytes."""
        key = generate_key()
        encrypted, _ = encrypt_file(b"test content", key)
        assert encrypted[:4] == b"MNME"

    def test_is_encrypted_true(self):
        """is_encrypted returns True for encrypted content."""
        key = generate_key()
        encrypted, _ = encrypt_file(b"test", key)
        assert is_encrypted(encrypted) is True

    def test_is_encrypted_false_plain(self):
        """is_encrypted returns False for plain text."""
        assert is_encrypted(b"plain text") is False

    def test_is_encrypted_false_empty(self):
        """is_encrypted returns False for empty content."""
        assert is_encrypted(b"") is False

    def test_is_encrypted_false_short(self):
        """is_encrypted returns False for short content that starts with MNME."""
        assert is_encrypted(b"MNME") is False

    def test_decrypt_with_magic_roundtrip(self):
        """Full roundtrip with magic header."""
        key = generate_key()
        original = "Hello 世界".encode("utf-8")
        encrypted, _ = encrypt_file(original, key)
        assert decrypt_file(encrypted, key) == original
