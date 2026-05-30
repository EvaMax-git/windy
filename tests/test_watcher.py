"""Tests for file watcher — A-34/A-35."""

import pytest
from pathlib import Path

from mneme.watcher.watcher import Watcher, FileEvent


@pytest.fixture
def watch_dir(tmp_path):
    """Create a watch directory with public/private subdirs."""
    d = tmp_path / "watch"
    d.mkdir()
    (d / "public").mkdir()
    (d / "private").mkdir()
    return d


@pytest.fixture
def watcher(watch_dir):
    """Create a Watcher instance with test config."""
    return Watcher(watch_dir=watch_dir, interval=1)


class TestWatcherInit:
    """Tests for Watcher initialization."""

    def test_creates_subdirectories(self, watch_dir):
        """Should create public/private subdirs on init."""
        Watcher(watch_dir=watch_dir, interval=1)
        assert (watch_dir / "public").is_dir()
        assert (watch_dir / "private").is_dir()

    def test_state_file_location(self, watcher, watch_dir):
        """State file should be in watch directory."""
        assert watcher._state_file == watch_dir / ".watched"


class TestDeduplication:
    """Tests for content-hash deduplication — A-35."""

    def test_same_file_not_processed_twice(self, watcher, watch_dir):
        """Same file should only be detected once."""
        f = watch_dir / "public" / "doc.txt"
        f.write_text("Hello, World!")

        # First scan
        events = watcher.scan_once()
        assert len(events) == 1
        assert events[0].path == f

        # Second scan — should not detect again
        events = watcher.scan_once()
        assert len(events) == 0

    def test_same_content_different_files(self, watcher, watch_dir):
        """Files with same content should be deduplicated."""
        (watch_dir / "public" / "a.txt").write_text("same content")
        (watch_dir / "public" / "b.txt").write_text("same content")

        events = watcher.scan_once()
        assert len(events) == 1  # Only one should be detected

    def test_different_files_detected(self, watcher, watch_dir):
        """Different files should all be detected."""
        (watch_dir / "public" / "a.txt").write_text("content a")
        (watch_dir / "public" / "b.txt").write_text("content b")

        events = watcher.scan_once()
        assert len(events) == 2

    def test_state_persists(self, watcher, watch_dir):
        """State should persist across scans."""
        f = watch_dir / "public" / "doc.txt"
        f.write_text("persistent content")

        watcher.scan_once()

        # Create new watcher with same state file
        watcher2 = Watcher(
            watch_dir=watch_dir,
            interval=1,
            state_file=watcher._state_file,
        )

        # Should not re-detect
        events = watcher2.scan_once()
        assert len(events) == 0


class TestFileFiltering:
    """Tests for file type filtering."""

    def test_supported_extensions(self, watcher, watch_dir):
        """Should detect supported file types."""
        (watch_dir / "public" / "doc.pdf").write_bytes(b"%PDF")
        (watch_dir / "public" / "text.txt").write_text("text")
        (watch_dir / "public" / "readme.md").write_text("# Title")

        events = watcher.scan_once()
        assert len(events) == 3

    def test_unsupported_extensions_skipped(self, watcher, watch_dir):
        """Should skip unsupported file types."""
        (watch_dir / "public" / "data.json").write_text("{}")
        (watch_dir / "public" / "script.py").write_text("print()")

        events = watcher.scan_once()
        assert len(events) == 0

    def test_hidden_files_skipped(self, watcher, watch_dir):
        """Should skip hidden files."""
        (watch_dir / "public" / ".hidden").write_text("hidden")
        (watch_dir / "public" / "visible.txt").write_text("visible")

        events = watcher.scan_once()
        assert len(events) == 1
        assert events[0].path.name == "visible.txt"

    def test_state_file_skipped(self, watcher, watch_dir):
        """Should skip the .watched state file."""
        (watch_dir / ".watched").write_text("state")
        (watch_dir / "public" / "doc.txt").write_text("doc")

        events = watcher.scan_once()
        assert len(events) == 1


class TestPrivateDetection:
    """Tests for private directory detection."""

    def test_public_file_not_private(self, watcher, watch_dir):
        """Files in public/ should not be marked private."""
        f = watch_dir / "public" / "doc.txt"
        f.write_text("public")

        events = watcher.scan_once()
        assert len(events) == 1
        assert events[0].is_private is False

    def test_private_file_is_private(self, watcher, watch_dir):
        """Files in private/ should be marked private."""
        f = watch_dir / "private" / "secret.txt"
        f.write_text("secret")

        events = watcher.scan_once()
        assert len(events) == 1
        assert events[0].is_private is True


class TestFileEvent:
    """Tests for FileEvent dataclass."""

    def test_event_fields(self, watcher, watch_dir):
        """Event should have correct fields."""
        f = watch_dir / "public" / "doc.txt"
        f.write_text("content")

        events = watcher.scan_once()
        event = events[0]

        assert event.path == f
        assert event.size_bytes == 7  # len("content")
        assert event.is_private is False
        assert len(event.content_hash) == 64  # SHA-256 hex
        assert event.timestamp > 0
