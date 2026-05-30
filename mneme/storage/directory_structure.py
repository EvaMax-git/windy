"""Public/Private directory structure management.

Provides:
- Directory initialization for public/private layout
- Path resolution with security checks
- Directory listing and status
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from mneme.config import get_settings

logger = logging.getLogger("mneme.storage.directory_structure")


# Directory names
PUBLIC_DIR = "public"
PRIVATE_DIR = "private"
STAGING_DIR = "staging"
KEYS_DIR = "keys"
WATCH_DIR = "watch"


@dataclass
class DirectoryStatus:
    """Status of a storage directory."""

    path: Path
    exists: bool
    file_count: int
    total_size_bytes: int


def get_storage_root() -> Path:
    """Get the resolved storage root path."""
    settings = get_settings()
    return Path(settings.storage_root).resolve()


def ensure_directory_structure(root: Path | None = None) -> dict[str, Path]:
    """Create the standard directory structure under the storage root.

    Structure:
        <root>/
        ├── public/      # Files accessible without encryption
        ├── private/     # Encrypted files (AES-256-GCM at rest)
        ├── staging/     # Temporary upload staging area
        ├── keys/        # Encryption key storage
        └── watch/       # Watcher auto-import directory
            ├── public/  # Drop zone for public files
            └── private/ # Drop zone for private (auto-encrypted) files

    Returns:
        Dict mapping directory names to their absolute paths.
    """
    base = root or get_storage_root()

    dirs = {
        "root": base,
        "public": base / PUBLIC_DIR,
        "private": base / PRIVATE_DIR,
        "staging": base / STAGING_DIR,
        "keys": base / KEYS_DIR,
        "watch": base / WATCH_DIR,
        "watch_public": base / WATCH_DIR / PUBLIC_DIR,
        "watch_private": base / WATCH_DIR / PRIVATE_DIR,
    }

    for name, path in dirs.items():
        path.mkdir(parents=True, exist_ok=True)

    return dirs


def resolve_path(relative_path: str, root: Path | None = None) -> Path:
    """Resolve a relative path under the storage root with security checks.

    Args:
        relative_path: Path relative to storage root (e.g. "public/doc.pdf").
        root: Storage root override (default: from config).

    Returns:
        Resolved absolute path.

    Raises:
        ValueError: If the path is unsafe (traversal, absolute, etc.).
    """
    if not relative_path:
        raise ValueError("Path must not be empty")

    base = root or get_storage_root()

    # Security checks
    if "\0" in relative_path:
        raise ValueError("Path contains null byte")

    # Reject absolute paths (Unix, Windows drive-letter, UNC)
    if relative_path.startswith("/") or relative_path.startswith("\\"):
        raise ValueError("Path must be relative")
    if os.path.isabs(relative_path):
        raise ValueError("Path must be relative")

    # Check for traversal before normalization
    parts = relative_path.replace("\\", "/").split("/")
    if ".." in parts:
        raise ValueError("Path traversal detected")

    resolved = (base / relative_path).resolve()

    # Ensure resolved path is under root using proper path relationship check
    # (not string prefix, which can be bypassed)
    if not resolved.is_relative_to(base):
        raise ValueError("Path resolves outside storage root")

    return resolved


def is_private_path(file_path: Path, root: Path | None = None) -> bool:
    """Check if a file is in the private directory.

    Uses case-insensitive comparison to handle case-insensitive filesystems
    (Windows, macOS HFS+).
    """
    base = root or get_storage_root()
    try:
        rel = file_path.resolve().relative_to(base)
        return rel.parts[0].lower() == PRIVATE_DIR if rel.parts else False
    except (ValueError, IndexError):
        return False


def list_directory_status(root: Path | None = None) -> dict[str, DirectoryStatus]:
    """Get status of all storage directories.

    Returns:
        Dict mapping directory names to their status.
    """
    base = root or get_storage_root()
    dirs = ensure_directory_structure(base)

    result = {}
    for name, path in dirs.items():
        if not path.is_dir():
            result[name] = DirectoryStatus(
                path=path, exists=False, file_count=0, total_size_bytes=0
            )
            continue

        file_count = 0
        total_size = 0
        for entry in path.rglob("*"):
            try:
                if entry.is_file():
                    file_count += 1
                    total_size += entry.stat().st_size
            except OSError as e:
                # File may have been deleted between rglob and stat (TOCTOU)
                logger.debug("Skipping file during status scan: %s", e)
                continue

        result[name] = DirectoryStatus(
            path=path, exists=True, file_count=file_count, total_size_bytes=total_size
        )

    return result
