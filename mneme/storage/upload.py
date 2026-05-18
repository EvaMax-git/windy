"""Idempotent upload handler with content-hash deduplication.

The ``handle_idempotent_upload`` function orchestrates the full upload
flow for a file and is the recommended entry-point for API consumers:

1. Validate file size and MIME type.
2. Stage the file to disk.
3. Check whether another asset with the same ``content_hash`` already exists
   (content-hash dedup → idempotent upload).
4. Return a :class:`IdempotentUploadResult` that the caller can use to
   either (a) create a new asset + promote, or (b) return the existing asset.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO
from uuid import UUID

from mneme.config import get_settings
from mneme.storage.backend import StorageBackend, get_backend
from mneme.storage.staging import (
    stage_file,
    stage_file_from_stream,
    compute_content_hash_bytes,
    StagedFileInfo,
)
from mneme.schemas.storage import ContentHashDuplicate


@dataclass(frozen=True)
class IdempotentUploadResult:
    """Result of processing an upload request.

    Attributes
    ----------
    is_duplicate : bool
        ``True`` if the content hash matches an existing asset.
    staged_info : StagedFileInfo | None
        Metadata for the newly staged file (``None`` for duplicates).
    duplicate_info : ContentHashDuplicate | None
        Information about the existing duplicate asset (``None`` for new files).
    """

    is_duplicate: bool
    staged_info: StagedFileInfo | None = None
    duplicate_info: ContentHashDuplicate | None = None


# ═══════════════════════════════════════════════════════════════════
# Validation
# ═══════════════════════════════════════════════════════════════════


def validate_upload_size(size_bytes: int) -> None:
    """Raise ``ValueError`` if *size_bytes* exceeds the configured maximum."""
    settings = get_settings()
    if size_bytes > settings.max_upload_size_bytes:
        raise ValueError(
            f"File size {size_bytes} bytes exceeds maximum "
            f"{settings.max_upload_size_bytes} bytes "
            f"({settings.max_upload_size_bytes // (1024 * 1024)} MB)"
        )
    if size_bytes <= 0:
        raise ValueError("File is empty")


def validate_mime_type(media_type: str | None) -> None:
    """Raise ``ValueError`` if *media_type* is not in the allowed list."""
    if media_type is None:
        return  # Allow unknown types (will be caught if needed upstream)
    settings = get_settings()
    allowed = settings.allowed_mime_types_list
    if media_type not in allowed:
        # Allow wildcard matches (e.g. "image/*")
        type_prefix = media_type.split("/")[0] + "/*"
        if type_prefix not in allowed and "*/*" not in allowed:
            raise ValueError(
                f"MIME type '{media_type}' is not allowed. "
                f"Allowed types: {', '.join(allowed[:5])}..."
            )


def validate_filename(filename: str) -> str:
    """Validate and sanitise *filename*.

    Returns the sanitised version.
    Raises ``ValueError`` if the filename is empty or contains path
    traversal patterns.
    """
    if not filename or not filename.strip():
        raise ValueError("Filename must not be empty")

    name = filename.strip()

    # Reject path traversal patterns
    if ".." in name or name.startswith("/") or name.startswith("\\"):
        raise ValueError(
            f"Filename contains unsafe path characters: {filename!r}"
        )

    # Reject null bytes
    if "\0" in name:
        raise ValueError("Filename contains null bytes")

    return name


# ═══════════════════════════════════════════════════════════════════
# Content-hash dedup lookup
# ═══════════════════════════════════════════════════════════════════


def _lookup_existing_by_content_hash(
    content_hash: str,
    project_id: UUID | None = None,
) -> ContentHashDuplicate | None:
    """Check whether an asset with *content_hash* already exists in the database.

    This function depends on the database layer (``mneme.db.assets``).
    It is called **after** the file has been staged, so that the hash
    is available.

    .. note::
       This is a *leaf* function that needs a database session.
       The public ``handle_idempotent_upload`` accepts a callback
       to avoid coupling the storage layer to the database.

    Returns ``None`` if no duplicate is found.
    """
    # This is a placeholder — the actual lookup is done by the DB layer.
    # The public API accepts a ``lookup_duplicate`` callback.
    return None


# ═══════════════════════════════════════════════════════════════════
# Main upload handler
# ═══════════════════════════════════════════════════════════════════


def handle_idempotent_upload_bytes(
    *,
    file_content: bytes,
    original_filename: str,
    project_id: UUID | None = None,
    lookup_duplicate=None,  # Callable[[str, UUID|None], ContentHashDuplicate|None]
    backend: StorageBackend | None = None,
    skip_mime_validation: bool = False,
) -> IdempotentUploadResult:
    """Handle an upload from raw bytes, with content-hash dedup.

    1. Validate that *file_content* is non-empty and within size limits.
    2. Compute hash and detect MIME type.
    3. If *lookup_duplicate* is provided, check for existing assets
       with the same content hash.
       - If found → return ``IdempotentUploadResult(is_duplicate=True, ...)``
       - The staged file is **not** written to disk in the duplicate case.
    4. Otherwise → stage the file and return ``is_duplicate=False``.

    Args:
        file_content: The full file content as bytes.
        original_filename: The original filename (will be sanitised).
        project_id: Optional project ID for scoped dedup.
        lookup_duplicate: ``Callable(content_hash, project_id) → ContentHashDuplicate | None``
        backend: Storage backend (uses singleton if ``None``).
        skip_mime_validation: If ``True``, skip MIME type validation.

    Returns:
        :class:`IdempotentUploadResult`
    """
    if not file_content:
        raise ValueError("file_content must not be empty")

    validate_filename(original_filename)
    validate_upload_size(len(file_content))

    # Compute hash first (before staging) so we can dedup without writing to disk
    content_hash = compute_content_hash_bytes(file_content)

    # Check for duplicate
    if lookup_duplicate is not None:
        existing = lookup_duplicate(content_hash, project_id)
        if existing is not None:
            return IdempotentUploadResult(
                is_duplicate=True,
                duplicate_info=existing,
            )

    # Stage the file
    staged_info = stage_file(
        file_content=file_content,
        original_filename=original_filename,
        backend=backend,
    )

    # Validate MIME type after detection
    if not skip_mime_validation:
        validate_mime_type(staged_info.media_type)

    return IdempotentUploadResult(
        is_duplicate=False,
        staged_info=staged_info,
    )


def handle_idempotent_upload_stream(
    *,
    stream: BinaryIO,
    original_filename: str,
    project_id: UUID | None = None,
    lookup_duplicate=None,
    backend: StorageBackend | None = None,
    skip_mime_validation: bool = False,
    chunk_size: int = 64 * 1024,
) -> IdempotentUploadResult:
    """Handle an upload from a binary stream, with content-hash dedup.

    For large files, the file is staged to disk first (so we can hash
    it incrementally), then the hash is checked for duplicates.

    .. warning::
       For stream-based uploads, the file must be written to disk first
       to compute its hash.  If a duplicate is found afterwards, the
       staged file is deleted automatically.

    Args:
        stream: Binary file-like object.
        original_filename: The original filename.
        project_id: Optional project ID for scoped dedup.
        lookup_duplicate: ``Callable(content_hash, project_id) → ContentHashDuplicate | None``
        backend: Storage backend.
        skip_mime_validation: If ``True``, skip MIME type validation.
        chunk_size: Read chunk size in bytes.

    Returns:
        :class:`IdempotentUploadResult`
    """
    validate_filename(original_filename)

    b = backend or get_backend()

    # Stage the file first (necessary to compute hash for large streams)
    staged_info = stage_file_from_stream(
        stream=stream,
        original_filename=original_filename,
        backend=b,
        chunk_size=chunk_size,
    )

    # Validate size
    validate_upload_size(staged_info.size_bytes)

    # Check for duplicate
    if lookup_duplicate is not None:
        existing = lookup_duplicate(staged_info.content_hash, project_id)
        if existing is not None:
            # Clean up the staged file — it's a duplicate
            b.delete_file(Path(staged_info.staging_path))
            return IdempotentUploadResult(
                is_duplicate=True,
                duplicate_info=existing,
            )

    # Validate MIME type after detection
    if not skip_mime_validation:
        validate_mime_type(staged_info.media_type)

    return IdempotentUploadResult(
        is_duplicate=False,
        staged_info=staged_info,
    )


# Re-export for convenience
compute_content_hash = compute_content_hash_bytes
