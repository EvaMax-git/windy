"""Tests for batch encryption/decryption — A-37."""

import pytest
from pathlib import Path

from mneme.security.file_encrypt import encrypt_file, generate_key, is_encrypted
from mneme.security.batch_encrypt import (
    batch_encrypt,
    batch_decrypt,
    encrypt_directory_tree,
    BatchResult,
)


@pytest.fixture
def sample_dir(tmp_path):
    """Create a sample directory with test files."""
    d = tmp_path / "test_files"
    d.mkdir()
    (d / "file1.txt").write_text("Hello, World!")
    (d / "file2.txt").write_text("你好世界")
    (d / "data.pdf").write_bytes(b"%PDF-1.4 fake content")
    return d


@pytest.fixture
def sample_tree(tmp_path):
    """Create a sample directory tree with public/private structure."""
    root = tmp_path / "storage"
    root.mkdir()
    public = root / "public"
    public.mkdir()
    private = root / "private"
    private.mkdir()
    (public / "readme.txt").write_text("Public file")
    (private / "secret.txt").write_text("Secret content")
    (private / "secret2.txt").write_text("Another secret")
    return root


class TestBatchResult:
    """Tests for BatchResult dataclass."""

    def test_defaults(self):
        result = BatchResult()
        assert result.processed == 0
        assert result.skipped == 0
        assert result.errors == []
        assert result.success_count == 0
        assert result.error_count == 0
        assert result.total == 0

    def test_with_values(self):
        result = BatchResult(processed=5, skipped=2, errors=[("a", "err")])
        assert result.success_count == 5
        assert result.error_count == 1
        assert result.total == 8


class TestBatchEncrypt:
    """Tests for batch_encrypt()."""

    def test_encrypts_all_files(self, sample_dir, key=None):
        """All files in directory should be encrypted."""
        k = generate_key()
        result = batch_encrypt(sample_dir, k)
        assert result.processed == 3
        assert result.error_count == 0

        # Verify files are encrypted
        for f in sample_dir.iterdir():
            if f.is_file():
                assert is_encrypted(f.read_bytes())

    def test_skips_already_encrypted(self, sample_dir):
        """Already encrypted files should be skipped."""
        k = generate_key()
        # First pass: encrypt all
        batch_encrypt(sample_dir, k)

        # Second pass: should skip all
        result = batch_encrypt(sample_dir, k)
        assert result.processed == 0
        assert result.skipped == 3

    def test_recursive(self, tmp_path):
        """Should process subdirectories when recursive=True."""
        k = generate_key()
        d = tmp_path / "test"
        d.mkdir()
        sub = d / "sub"
        sub.mkdir()
        (d / "a.txt").write_text("file a")
        (sub / "b.txt").write_text("file b")

        result = batch_encrypt(d, k, recursive=True)
        assert result.processed == 2

    def test_non_recursive(self, tmp_path):
        """Should only process top-level files when recursive=False."""
        k = generate_key()
        d = tmp_path / "test"
        d.mkdir()
        sub = d / "sub"
        sub.mkdir()
        (d / "a.txt").write_text("file a")
        (sub / "b.txt").write_text("file b")

        result = batch_encrypt(d, k, recursive=False)
        assert result.processed == 1

    def test_invalid_key_raises(self, sample_dir):
        """Key must be 32 bytes."""
        with pytest.raises(ValueError, match="32 bytes"):
            batch_encrypt(sample_dir, b"short")

    def test_not_a_directory_raises(self, tmp_path):
        """Must be a directory."""
        f = tmp_path / "file.txt"
        f.write_text("test")
        with pytest.raises(ValueError, match="Not a directory"):
            batch_encrypt(f, generate_key())

    def test_skips_hidden_files(self, tmp_path):
        """Hidden files should be skipped."""
        k = generate_key()
        d = tmp_path / "test"
        d.mkdir()
        (d / "visible.txt").write_text("visible")
        (d / ".hidden").write_text("hidden")

        result = batch_encrypt(d, k)
        assert result.processed == 1
        assert result.skipped == 0  # hidden files are skipped, not counted


class TestBatchDecrypt:
    """Tests for batch_decrypt()."""

    def test_decrypts_all_encrypted(self, sample_dir):
        """All encrypted files should be decrypted."""
        k = generate_key()
        batch_encrypt(sample_dir, k)
        result = batch_decrypt(sample_dir, k)
        assert result.processed == 3

        # Verify files are decrypted
        assert (sample_dir / "file1.txt").read_text() == "Hello, World!"
        assert (sample_dir / "file2.txt").read_text() == "你好世界"

    def test_skips_unencrypted(self, sample_dir):
        """Unencrypted files should be skipped."""
        k = generate_key()
        result = batch_decrypt(sample_dir, k)
        assert result.processed == 0
        assert result.skipped == 3


class TestEncryptDirectoryTree:
    """Tests for encrypt_directory_tree()."""

    def test_encrypts_private_only(self, sample_tree):
        """Only private directory files should be encrypted."""
        k = generate_key()
        results = encrypt_directory_tree(sample_tree, k)

        assert "private" in results
        assert results["private"].processed == 2

        # Public files should NOT be encrypted
        public_file = sample_tree / "public" / "readme.txt"
        assert not is_encrypted(public_file.read_bytes())

        # Private files should be encrypted
        private_file = sample_tree / "private" / "secret.txt"
        assert is_encrypted(private_file.read_bytes())

    def test_missing_private_dir(self, tmp_path):
        """Should handle missing private directory gracefully."""
        k = generate_key()
        root = tmp_path / "empty"
        root.mkdir()
        results = encrypt_directory_tree(root, k)
        assert results["private"].processed == 0
