"""Tests for AES-256-GCM file encryption — A-17~A-19."""

import pytest

from mneme.security.file_encrypt import decrypt_file, encrypt_file, generate_key


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
        encrypted = encrypt_file(content, key)
        decrypted = decrypt_file(encrypted, key)
        assert decrypted == content

    def test_encrypted_is_different(self):
        """Ciphertext must differ from plaintext."""
        content = b"Hello, Mneme!"
        key = generate_key()
        encrypted = encrypt_file(content, key)
        assert encrypted != content

    def test_wrong_key_fails(self):
        """Decrypting with wrong key must raise ValueError."""
        content = b"Hello, Mneme!"
        key = generate_key()
        wrong_key = generate_key()
        encrypted = encrypt_file(content, key)
        with pytest.raises(ValueError, match="解密失败"):
            decrypt_file(encrypted, wrong_key)

    def test_chinese_content(self):
        """Chinese text roundtrip must succeed."""
        content = "你好世界，这是中文测试内容。".encode("utf-8")
        key = generate_key()
        encrypted = encrypt_file(content, key)
        decrypted = decrypt_file(encrypted, key)
        assert decrypted == content

    def test_empty_content_fails(self):
        """Empty bytes must raise ValueError."""
        key = generate_key()
        with pytest.raises(ValueError, match="content must not be empty"):
            encrypt_file(b"", key)
