"""File promotion — atomically move a staged file into permanent Asset storage.

The ``promote_file`` function is the bridge between staging (temporary)
and the final ``mneme_data/assets/{project_id}/{asset_uid}/`` directory.
It must be called **inside a database transaction** that also updates
``assets.storage_ref`` and advances ``assets.ingest_state``.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from mneme.storage.backend import (
    StorageBackend,
    get_backend,
    sanitize_filename,
)


class PromoteError(Exception):
    """Raised when file promotion fails (missing source, destination conflict, etc.)."""


def promote_file(
    *,
    staging_path: str | Path,
    project_id: UUID | None = None,
    asset_uid: str,
    original_filename: str,
    backend: StorageBackend | None = None,
) -> str:
    """Move a staged file to its permanent asset directory.

    The destination path follows the convention::

        <storage_root>/assets/<project_id>/<asset_uid>/<sanitized_filename>

    Args:
        staging_path: Absolute path to the staged file.
        project_id: UUID of the owning project.
        asset_uid: Unique asset identifier (e.g. ``"prj-abc123-1234567890"``).
        original_filename: Sanitised original filename.
        backend: Storage backend (uses singleton if ``None``).

    Returns:
        The absolute ``storage_ref`` path of the promoted file.

    Raises:
        PromoteError: If the staged file does not exist or the move fails.
    """
    b = backend or get_backend()
    src = Path(staging_path)

    if not src.is_absolute():
        raise PromoteError(f"staging_path must be absolute, got: {staging_path}")
    if not b.file_exists(src):
        raise PromoteError(f"Staged file does not exist: {staging_path}")

    safe_name = sanitize_filename(original_filename)
    project_dir = str(project_id) if project_id else "default"
    asset_dir = b.storage_root / "assets" / project_dir / asset_uid
    dst = asset_dir / safe_name

    # Guard: the destination must not already exist
    if b.file_exists(dst):
        raise PromoteError(
            f"Destination already exists: {dst}. "
            f"This indicates a duplicate asset_uid or a previous failed cleanup."
        )

    # Ensure the asset directory tree exists
    b.ensure_directory(asset_dir)

    # Atomic move
    b.move_file(src, dst)

    # Verify the file arrived
    if not b.file_exists(dst):
        raise PromoteError(f"File not found at destination after move: {dst}")

    return str(dst)


def _build_asset_path(
    project_id: UUID | None,
    asset_uid: str,
    original_filename: str,
    backend: StorageBackend | None = None,
) -> str:
    """Return the canonical storage_ref path that *would* be created by
    :func:`promote_file` without actually performing the move.

    Used for pre-computing ``storage_ref`` values before the promote
    transaction is committed.
    """
    b = backend or get_backend()
    safe_name = sanitize_filename(original_filename)
    project_dir = str(project_id) if project_id else "default"
    asset_dir = b.storage_root / "assets" / project_dir / asset_uid
    return str(asset_dir / safe_name)


def rollback_promote(
    storage_ref: str,
    backend: StorageBackend | None = None,
) -> None:
    """Delete a previously promoted file (used during transaction rollback).

    Unlike database rollback, filesystem operations are not transactional.
    This function is called when the caller detects that the database
    transaction around :func:`promote_file` has been rolled back.

    Args:
        storage_ref: Absolute path to the promoted file.
        backend: Storage backend (uses singleton if ``None``).
    """
    b = backend or get_backend()
    path = Path(storage_ref)
    if b.file_exists(path):
        b.delete_file(path)
    # Also remove the parent directory if empty
    try:
        parent = path.parent
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            # Try to remove grandparent (project_id dir) if empty
            grandparent = parent.parent
            if grandparent.is_dir() and not any(grandparent.iterdir()):
                grandparent.rmdir()
    except OSError:
        pass
