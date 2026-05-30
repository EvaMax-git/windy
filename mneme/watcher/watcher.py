"""File watcher — monitors a directory for new files and auto-imports them.

Features:
- Polling-based directory monitoring (cross-platform, no OS-specific deps)
- Content-hash deduplication (same file won't be processed twice)
- Public/private directory awareness
- Graceful shutdown on SIGINT/SIGTERM
"""

from __future__ import annotations

import hashlib
import logging
import os
import signal
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from mneme.config import get_settings

logger = logging.getLogger("mneme.watcher")

# File extensions we can process
_SUPPORTED_EXTENSIONS = frozenset({
    ".pdf", ".docx", ".doc", ".txt", ".md",
    ".png", ".jpg", ".jpeg", ".webp", ".bmp",
})

# Directories to ignore
_IGNORED_DIRS = frozenset({".git", "__pycache__", "node_modules"})

# Stability check: wait this many seconds between size checks
_STABILITY_SECONDS = 1.0


@dataclass
class FileEvent:
    """Represents a new file detected by the watcher."""

    path: Path
    content_hash: str
    size_bytes: int
    is_private: bool = False
    timestamp: float = field(default_factory=time.time)


class Watcher:
    """Polling-based file system watcher with content-hash deduplication.

    Args:
        watch_dir: Directory to monitor for new files.
        interval: Poll interval in seconds (default: 5).
        state_file: Path to the deduplication state file (default: auto).
    """

    def __init__(
        self,
        watch_dir: str | Path | None = None,
        interval: int | None = None,
        state_file: str | Path | None = None,
    ) -> None:
        self._watch_dir = Path(
            watch_dir if watch_dir is not None
            else os.environ.get("MNEME_WATCH_DIR", "mneme_data/watch")
        )
        self._interval = (
            interval if interval is not None
            else int(os.environ.get("MNEME_WATCH_INTERVAL", "5"))
        )
        self._state_file = Path(
            state_file if state_file is not None
            else os.environ.get("MNEME_WATCH_STATE", str(self._watch_dir / ".watched"))
        )

        # Deduplication: content_hash -> file_path
        self._seen_hashes: dict[str, str] = {}
        # Thread-safe running flag
        self._stop_event = threading.Event()

        # Load persisted state
        self._load_state()

    # ── Public API ────────────────────────────────────────────────

    def start(self) -> None:
        """Start the watcher loop. Blocks until stopped."""
        self._ensure_dirs()
        self._stop_event.clear()

        # Register signal handlers for graceful shutdown
        # SIGTERM is not catchable on Windows
        if sys.platform != "win32":
            signal.signal(signal.SIGTERM, self._handle_signal)
        try:
            signal.signal(signal.SIGINT, self._handle_signal)
        except (ValueError, OSError):
            # Cannot set signal handler from non-main thread
            logger.debug("Cannot set SIGINT handler (not main thread)")

        logger.info("Watcher started: dir=%s interval=%ds", self._watch_dir, self._interval)

        while not self._stop_event.is_set():
            try:
                self._scan_once()
            except Exception:
                logger.exception("Error during scan cycle")
            self._stop_event.wait(timeout=self._interval)

        logger.info("Watcher stopped.")

    def stop(self) -> None:
        """Stop the watcher loop."""
        self._stop_event.set()

    def scan_once(self) -> list[FileEvent]:
        """Run a single scan cycle and return detected events.

        Useful for testing or one-shot imports.
        """
        self._ensure_dirs()
        return self._scan_once()

    @property
    def seen_hashes(self) -> dict[str, str]:
        """Return a copy of the seen content hashes."""
        return dict(self._seen_hashes)

    # ── Internal ──────────────────────────────────────────────────

    def _ensure_dirs(self) -> None:
        """Ensure watch directory and subdirectories exist."""
        self._watch_dir.mkdir(parents=True, exist_ok=True)
        (self._watch_dir / "public").mkdir(exist_ok=True)
        (self._watch_dir / "private").mkdir(exist_ok=True)

    def _handle_signal(self, signum: int, frame: object) -> None:
        """Handle SIGINT/SIGTERM for graceful shutdown."""
        logger.info("Received signal %d, shutting down...", signum)
        self._stop_event.set()

    def _compute_hash(self, file_path: Path) -> str:
        """Compute SHA-256 hash of a file."""
        h = hashlib.sha256()
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(64 * 1024)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()

    def _is_supported(self, file_path: Path) -> bool:
        """Check if the file has a supported extension."""
        return file_path.suffix.lower() in _SUPPORTED_EXTENSIONS

    def _should_ignore(self, file_path: Path) -> bool:
        """Check if the file should be ignored."""
        name = file_path.name
        if name.startswith(".") or name == ".watched":
            return True
        parts = file_path.parts
        for part in parts:
            if part in _IGNORED_DIRS:
                return True
        return False

    def _is_private(self, file_path: Path) -> bool:
        """Check if the file is in the private directory."""
        try:
            rel = file_path.relative_to(self._watch_dir)
            return rel.parts[0].lower() == "private" if rel.parts else False
        except (ValueError, IndexError):
            return False

    def _check_stability_batch(self, candidates: list[tuple[Path, int]]) -> list[Path]:
        """Check file stability for a batch of candidates.

        Records sizes, sleeps once, then re-checks. Returns only stable files.
        This is O(1) sleep instead of O(N).
        """
        if not candidates:
            return []

        # Record initial sizes
        initial_sizes: dict[Path, int] = {path: size for path, size in candidates}

        # Sleep once for all files
        time.sleep(_STABILITY_SECONDS)

        # Re-check sizes
        stable: list[Path] = []
        for path, initial_size in candidates:
            try:
                current_size = path.stat().st_size
                if current_size == initial_size and current_size > 0:
                    stable.append(path)
                elif current_size != initial_size:
                    logger.debug("File size changed (still writing): %s", path.name)
            except OSError:
                logger.debug("File disappeared during stability check: %s", path.name)

        return stable

    def _scan_once(self) -> list[FileEvent]:
        """Scan the watch directory for new files."""
        events: list[FileEvent] = []

        # Phase 1: Collect candidates (filter by extension, ignore, empty)
        candidates: list[tuple[Path, int]] = []
        for file_path in self._walk_files():
            if self._should_ignore(file_path):
                continue
            if not self._is_supported(file_path):
                continue

            try:
                file_size = file_path.stat().st_size
            except OSError:
                continue
            if file_size == 0:
                continue

            candidates.append((file_path, file_size))

        # Phase 2: Batch stability check (one sleep for all files)
        stable_files = self._check_stability_batch(candidates)

        # Phase 3: Hash and deduplicate stable files
        for file_path in stable_files:
            try:
                content_hash = self._compute_hash(file_path)
            except OSError as e:
                logger.warning("Cannot read file %s: %s", file_path, e)
                continue

            file_key = str(file_path.resolve())
            if content_hash in self._seen_hashes:
                existing = self._seen_hashes[content_hash]
                if existing == file_key:
                    logger.debug("Already processed (same path): %s", file_path.name)
                    continue
                else:
                    logger.info(
                        "Duplicate content detected: %s (same as %s), skipping",
                        file_path.name,
                        Path(existing).name,
                    )
                    continue

            # New file detected
            event = FileEvent(
                path=file_path,
                content_hash=content_hash,
                size_bytes=file_path.stat().st_size,
                is_private=self._is_private(file_path),
            )
            events.append(event)

            # Mark as seen
            self._seen_hashes[content_hash] = file_key
            logger.info(
                "New file detected: %s (hash=%s..., size=%d, private=%s)",
                file_path.name,
                content_hash[:8],
                event.size_bytes,
                event.is_private,
            )

        # Persist state after scan
        if events:
            self._save_state()

        return events

    def _walk_files(self):
        """Walk the watch directory and yield all regular files (generator)."""
        try:
            for entry in self._watch_dir.rglob("*"):
                # Skip symlinks to avoid loops and unintended side effects
                if entry.is_file() and not entry.is_symlink():
                    yield entry
        except OSError as e:
            logger.warning("Error walking watch directory: %s", e)

    # ── State persistence ─────────────────────────────────────────

    def _load_state(self) -> None:
        """Load deduplication state from disk."""
        if not self._state_file.exists():
            return
        try:
            with open(self._state_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    # Use null byte as separator (cannot appear in file paths)
                    if "\0" in line:
                        parts = line.split("\0", 1)
                    else:
                        # Legacy format: tab-separated
                        parts = line.split("\t", 1)
                    if len(parts) == 2:
                        content_hash, file_path = parts
                        self._seen_hashes[content_hash] = file_path
            logger.debug("Loaded %d hashes from state file", len(self._seen_hashes))
        except OSError as e:
            logger.warning("Cannot load state file: %s", e)

    def _save_state(self) -> None:
        """Save deduplication state to disk atomically.

        Uses write-to-temp-then-rename pattern to prevent corruption
        on crash or concurrent access. Uses null byte separator to
        avoid issues with tab characters in file paths.
        """
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)

            # Write to a temporary file first
            fd, tmp_path = tempfile.mkstemp(
                dir=str(self._state_file.parent),
                prefix=".watched_tmp_",
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write("# Mneme watcher deduplication state\n")
                    for content_hash, file_path in sorted(self._seen_hashes.items()):
                        # Use null byte separator (safe for all file paths)
                        f.write(f"{content_hash}\0{file_path}\n")

                # Atomic rename (on same filesystem)
                os.replace(tmp_path, str(self._state_file))
                logger.debug("Saved %d hashes to state file", len(self._seen_hashes))
            except Exception:
                # Clean up temp file on failure
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except OSError as e:
            logger.warning("Cannot save state file: %s", e)
