"""P3-01 Storage layer — file-system backend, staging, promotion, and
idempotent upload with content-hash deduplication.

Exports
-------
* :func:`get_backend` — Factory that returns the configured ``StorageBackend``.
* :func:`stage_file` — Stage an uploaded file on disk, returning a ``StagedFileInfo``.
* :func:`promote_file` — Atomically move a staged file into a permanent asset directory.
* :func:`handle_idempotent_upload` — Full upload handler with content-hash dedup.
"""

from mneme.storage.backend import LocalFileSystemBackend, StorageBackend, get_backend
from mneme.storage.staging import stage_file, _cleanup_staging_dir, _stage_path_for
from mneme.storage.promote import promote_file, _build_asset_path
from mneme.storage.path_resolver import get_storage_path, check_nas_available
from mneme.storage.directory_structure import (
    ensure_directory_structure,
    resolve_path,
    is_private_path,
    list_directory_status,
    DirectoryStatus,
)
from mneme.storage.upload import (
    IdempotentUploadResult,
    handle_idempotent_upload_bytes,
    handle_idempotent_upload_stream,
    validate_upload_size,
    validate_mime_type,
    compute_content_hash,
)

# Convenience alias: handle_idempotent_upload → bytes-based handler
handle_idempotent_upload = handle_idempotent_upload_bytes

__all__ = [
    # Backend
    "StorageBackend",
    "LocalFileSystemBackend",
    "get_backend",
    # Staging
    "stage_file",
    "_cleanup_staging_dir",
    "_stage_path_for",
    # Promote
    "promote_file",
    "_build_asset_path",
    # Upload
    "IdempotentUploadResult",
    "handle_idempotent_upload",
    "handle_idempotent_upload_bytes",
    "handle_idempotent_upload_stream",
    "validate_upload_size",
    "validate_mime_type",
    "compute_content_hash",
    # Path Resolver
    "get_storage_path",
    "check_nas_available",
    # Directory Structure
    "ensure_directory_structure",
    "resolve_path",
    "is_private_path",
    "list_directory_status",
    "DirectoryStatus",
]
