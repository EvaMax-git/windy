"""Tests for public/private directory structure — A-36."""

import pytest
from pathlib import Path

from mneme.storage.directory_structure import (
    ensure_directory_structure,
    resolve_path,
    is_private_path,
    list_directory_status,
    DirectoryStatus,
    PUBLIC_DIR,
    PRIVATE_DIR,
    STAGING_DIR,
    KEYS_DIR,
    WATCH_DIR,
)


@pytest.fixture
def storage_root(tmp_path):
    """Create a temporary storage root."""
    return tmp_path / "storage"


class TestEnsureDirectoryStructure:
    """Tests for ensure_directory_structure()."""

    def test_creates_all_dirs(self, storage_root):
        """Should create all standard directories."""
        dirs = ensure_directory_structure(storage_root)

        assert dirs["root"] == storage_root
        assert (storage_root / PUBLIC_DIR).is_dir()
        assert (storage_root / PRIVATE_DIR).is_dir()
        assert (storage_root / STAGING_DIR).is_dir()
        assert (storage_root / KEYS_DIR).is_dir()
        assert (storage_root / WATCH_DIR).is_dir()
        assert (storage_root / WATCH_DIR / PUBLIC_DIR).is_dir()
        assert (storage_root / WATCH_DIR / PRIVATE_DIR).is_dir()

    def test_idempotent(self, storage_root):
        """Should be safe to call multiple times."""
        dirs1 = ensure_directory_structure(storage_root)
        dirs2 = ensure_directory_structure(storage_root)
        assert dirs1 == dirs2

    def test_existing_files_preserved(self, storage_root):
        """Should not delete existing files."""
        storage_root.mkdir(parents=True)
        (storage_root / "existing.txt").write_text("hello")
        ensure_directory_structure(storage_root)
        assert (storage_root / "existing.txt").read_text() == "hello"


class TestResolvePath:
    """Tests for resolve_path()."""

    def test_simple_path(self, storage_root):
        """Should resolve simple relative path."""
        ensure_directory_structure(storage_root)
        result = resolve_path("public/doc.pdf", storage_root)
        assert result == storage_root / "public" / "doc.pdf"

    def test_nested_path(self, storage_root):
        """Should resolve nested path."""
        ensure_directory_structure(storage_root)
        result = resolve_path("private/subdir/file.txt", storage_root)
        assert result == storage_root / "private" / "subdir" / "file.txt"

    def test_empty_path_raises(self, storage_root):
        """Empty path should raise ValueError."""
        with pytest.raises(ValueError, match="must not be empty"):
            resolve_path("", storage_root)

    def test_null_byte_raises(self, storage_root):
        """Null byte should raise ValueError."""
        with pytest.raises(ValueError, match="null byte"):
            resolve_path("public/file\x00.txt", storage_root)

    def test_absolute_path_raises(self, storage_root):
        """Absolute path should raise ValueError."""
        with pytest.raises(ValueError, match="must be relative"):
            resolve_path("/etc/passwd", storage_root)

    def test_traversal_raises(self, storage_root):
        """Path traversal should raise ValueError."""
        with pytest.raises(ValueError, match="traversal"):
            resolve_path("public/../../../etc/passwd", storage_root)

    def test_dot_dot_raises(self, storage_root):
        """Double dot should raise ValueError."""
        with pytest.raises(ValueError, match="traversal"):
            resolve_path("../secret", storage_root)


class TestIsPrivatePath:
    """Tests for is_private_path()."""

    def test_private_file(self, storage_root):
        """Files in private/ should be private."""
        ensure_directory_structure(storage_root)
        f = storage_root / "private" / "secret.txt"
        assert is_private_path(f, storage_root) is True

    def test_public_file(self, storage_root):
        """Files in public/ should not be private."""
        ensure_directory_structure(storage_root)
        f = storage_root / "public" / "readme.txt"
        assert is_private_path(f, storage_root) is False

    def test_root_file(self, storage_root):
        """Files at root should not be private."""
        ensure_directory_structure(storage_root)
        f = storage_root / "config.json"
        assert is_private_path(f, storage_root) is False

    def test_nested_private(self, storage_root):
        """Nested files in private/ should be private."""
        ensure_directory_structure(storage_root)
        f = storage_root / "private" / "subdir" / "secret.txt"
        assert is_private_path(f, storage_root) is True


class TestListDirectoryStatus:
    """Tests for list_directory_status()."""

    def test_empty_dirs(self, storage_root):
        """Empty directories should have zero counts."""
        ensure_directory_structure(storage_root)
        status = list_directory_status(storage_root)

        assert "public" in status
        assert "private" in status
        assert status["public"].file_count == 0
        assert status["public"].total_size_bytes == 0

    def test_with_files(self, storage_root):
        """Should count files and sizes correctly."""
        ensure_directory_structure(storage_root)
        (storage_root / "public" / "a.txt").write_text("hello")  # 5 bytes
        (storage_root / "public" / "b.txt").write_text("world!")  # 6 bytes
        (storage_root / "private" / "secret.txt").write_text("secret")  # 6 bytes

        status = list_directory_status(storage_root)
        assert status["public"].file_count == 2
        assert status["public"].total_size_bytes == 11
        assert status["private"].file_count == 1
        assert status["private"].total_size_bytes == 6
