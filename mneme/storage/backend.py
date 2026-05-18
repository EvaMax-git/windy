"""File-system storage backend abstraction.

Current implementation: ``LocalFileSystemBackend`` writing everything under
``mneme_data/``.  The backend enum ``StorageBackend.mneme_data`` maps to this.

Future backends (S3-compatible, etc.) should implement the same ``StorageBackend``
protocol and be selected via ``MNEME_STORAGE_BACKEND`` config.
"""

from __future__ import annotations

import os
import re
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID

from mneme.config import get_settings


# Unsafe filename characters: control chars, path separators, and typical
# reserved characters across OS (Windows especially).  Unicode letters,
# digits, dots, dashes, underscores, and spaces are permitted.
_UNSAFE_FILENAME = re.compile(r"[\x00-\x1f\x7f<>:\"/\\|?*\t\r\n]")


@dataclass(frozen=True)
class _StorageBackendProtocol(Protocol):
    """Structural protocol that all storage backends must satisfy.

    Each method type-checks the minimum interface required by the
    staging / promote / upload layers.
    """

    @property
    def storage_root(self) -> Path: ...
    def ensure_directory(self, path: Path) -> None: ...
    def write_file(self, path: Path, content: bytes) -> int: ...
    def read_file(self, path: Path) -> bytes: ...
    def move_file(self, src: Path, dst: Path) -> None: ...
    def delete_file(self, path: Path) -> None: ...
    def file_exists(self, path: Path) -> bool: ...
    def file_size(self, path: Path) -> int: ...


class StorageBackend(ABC):
    """Abstract file-system storage backend.

    Each concrete backend manages a root directory and provides
    basic file operations: ensure directories exist, write, read,
    move (promote), delete, and existence checks.
    """

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).resolve()

    @property
    def storage_root(self) -> Path:
        return self._root

    @abstractmethod
    def ensure_directory(self, path: Path) -> None:
        """Create *path* and all parent directories if they do not exist."""
        ...

    @abstractmethod
    def write_file(self, path: Path, content: bytes) -> int:
        """Write *content* to *path*, creating parent directories as needed.

        Returns the number of bytes written.
        """
        ...

    @abstractmethod
    def read_file(self, path: Path) -> bytes:
        """Read and return the full content of *path*."""
        ...

    @abstractmethod
    def move_file(self, src: Path, dst: Path) -> None:
        """Atomically move *src* to *dst*.

        Creates the destination parent directories if necessary.
        """
        ...

    @abstractmethod
    def delete_file(self, path: Path) -> None:
        """Delete *path* if it exists (silently succeeds if missing)."""
        ...

    @abstractmethod
    def file_exists(self, path: Path) -> bool:
        """Return ``True`` if *path* exists and is a regular file."""
        ...

    @abstractmethod
    def file_size(self, path: Path) -> int:
        """Return the size in bytes of *path*."""
        ...


class LocalFileSystemBackend(StorageBackend):
    """Concrete backend that writes to the local file-system.

    All paths are kept under the configured ``storage_root``
    (default: ``./mneme_data/``).  Directory trees are created
    automatically on write or move.
    """

    def ensure_directory(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)

    def write_file(self, path: Path, content: bytes) -> int:
        self.ensure_directory(path.parent)
        written = path.write_bytes(content)
        return len(content)

    def read_file(self, path: Path) -> bytes:
        return path.read_bytes()

    def move_file(self, src: Path, dst: Path) -> None:
        self.ensure_directory(dst.parent)
        # Use os.rename for atomic move on same filesystem;
        # fall back to shutil.move for cross-device moves.
        try:
            os.rename(src, dst)
        except OSError:
            shutil.move(str(src), str(dst))

    def delete_file(self, path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def file_exists(self, path: Path) -> bool:
        return path.is_file()

    def file_size(self, path: Path) -> int:
        return path.stat().st_size


# ═══════════════════════════════════════════════════════════════════
# Path utilities
# ═══════════════════════════════════════════════════════════════════


def sanitize_filename(filename: str) -> str:
    """Sanitize *filename* for safe storage on disk.

    - Strips leading/trailing whitespace.
    - Replaces path separators and ``..`` traversal with underscores.
    - Replaces characters that are not alphanumeric, dot, dash,
      underscore, or space with an underscore.
    - Collapses consecutive underscores.
    - Removes leading dots (hidden files).
    - Returns ``"unnamed"`` if the result is empty.
    """
    name = filename.strip()

    # Remove any path separators (prevent path traversal)
    name = name.replace("/", "_").replace("\\", "_")

    # Explicitly collapse ".." traversal strings
    while ".." in name:
        name = name.replace("..", "_")

    # Replace unsafe characters
    name = _UNSAFE_FILENAME.sub("_", name)

    # Collapse consecutive underscores
    name = re.sub(r"_+", "_", name)

    # Remove leading dots (hidden file marker)
    name = name.lstrip(".")

    # Trim to a reasonable length (max 255 bytes for most filesystems)
    name_bytes = name.encode("utf-8")
    if len(name_bytes) > 240:
        # Truncate while keeping extension if plausible
        dot_pos = name.rfind(".")
        if dot_pos > 0 and dot_pos > len(name) - 40:
            name = name[:200] + name[dot_pos:]
        else:
            name = name[:200]
        # Re-encode and truncate byte-wise
        name_bytes = name.encode("utf-8")
        while len(name_bytes) > 240:
            name = name[:-1]
            name_bytes = name.encode("utf-8")

    if not name:
        name = "unnamed"

    return name


def is_path_safe(relative_path: str) -> bool:
    """Return ``True`` if *relative_path* contains no traversal patterns.

    Checks for:
    - ``..`` (parent directory traversal) — checked BEFORE normalization
    - Absolute paths (starts with ``/``)
    - Null bytes
    """
    if not relative_path:
        return False
    if "\0" in relative_path:
        return False
    if relative_path.startswith("/"):
        return False

    # Split raw path to check for ".." segments before any normalization
    # (os.path.normpath would resolve "foo/../bar" → "bar", hiding the traversal)
    raw_parts = relative_path.replace("\\", "/").split("/")
    if ".." in raw_parts:
        return False

    # Normalize to catch ``./foo/../../bar`` style tricks that normpath may resolve
    normalized = os.path.normpath(relative_path)
    if normalized.startswith("..") or os.path.isabs(normalized):
        return False
    if ".." in normalized.split(os.sep):
        return False

    return True


def build_staging_path(staging_dir: str, filename: str) -> Path:
    """Build an absolute, sanitized staging path."""
    settings = get_settings()
    root = Path(settings.storage_root).resolve()
    return root / settings.staging_subdir / sanitize_filename(filename)


def build_asset_directory(project_id: UUID, asset_uid: str) -> Path:
    """Build the permanent asset directory path.

    Format: ``<storage_root>/assets/<project_id>/<asset_uid>/``
    """
    settings = get_settings()
    return (
        Path(settings.storage_root).resolve()
        / "assets"
        / str(project_id)
        / asset_uid
    )


# ═══════════════════════════════════════════════════════════════════
# Singleton backend factory
# ═══════════════════════════════════════════════════════════════════

_backend: StorageBackend | None = None


def get_backend() -> StorageBackend:
    """Return the singleton ``StorageBackend`` for the configured backend.

    Currently only ``LocalFileSystemBackend`` is implemented.
    """
    global _backend
    if _backend is None:
        settings = get_settings()
        if settings.storage_backend == "mneme_data":
            _backend = LocalFileSystemBackend(settings.storage_root)
        else:
            # Future: S3-compatible, etc.
            _backend = LocalFileSystemBackend(settings.storage_root)
    return _backend


def reset_backend() -> None:
    """Reset the backend singleton (used in tests)."""
    global _backend
    _backend = None
