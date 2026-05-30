"""Staging directory management.

Handles temporary storage of uploaded files before they are promoted
to permanent asset storage.  Includes MIME type detection, content
hashing, path sanitisation, and size validation.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import time
from pathlib import Path
from typing import BinaryIO

from mneme.config import get_settings
from mneme.storage.backend import (
    StorageBackend,
    get_backend,
    sanitize_filename,
    is_path_safe,
)
from mneme.schemas.storage import StagedFileInfo


# Initialise the built-in MIME type database
mimetypes.init()


# ═══════════════════════════════════════════════════════════════════
# MIME detection
# ═══════════════════════════════════════════════════════════════════


_MIME_MAGIC_CACHE: dict[int, bytes] = {}  # fd → first 2048 bytes for sniffing

# Common file signatures (magic bytes) for types that mimetypes guesses poorly
_MAGIC_SIGNATURES: dict[bytes, str] = {
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"\xff\xd8\xff": "image/jpeg",
    b"GIF87a": "image/gif",
    b"GIF89a": "image/gif",
    b"RIFF": "audio/wav",  # needs further check
    b"\x1f\x8b\x08": "application/gzip",
    b"PK\x03\x04": "application/zip",
    b"%PDF": "application/pdf",
    b"\xd0\xcf\x11\xe0": "application/msword",  # OLE2 (doc/xls)
    b"\x7fELF": "application/x-executable",
    b"#!\x2f": "text/x-script",  # shebang
}

# Extended RIFF detection
_RIFF_TYPES: dict[bytes, str] = {
    b"AVI ": "video/avi",
    b"WAVE": "audio/wav",
    b"WEBP": "image/webp",
}


def detect_mime_type(filename: str, content_head: bytes) -> str | None:
    """Detect the MIME type of *filename* using magic bytes and extension.

    Strategy:
    1. Check magic bytes against known signatures.
       For ZIP-based Office formats (.docx/.xlsx/.pptx), the magic bytes
       detect ``application/zip`` — we then correct via extension lookup.
    2. Fall back to ``mimetypes.guess_type`` by extension.
    3. Return ``"application/octet-stream"`` as last resort.
    """
    # 1. Magic byte detection
    for magic, mime in _MAGIC_SIGNATURES.items():
        if content_head.startswith(magic):
            # Special handling for RIFF containers
            if magic == b"RIFF" and len(content_head) >= 12:
                riff_type = content_head[8:12]
                if riff_type in _RIFF_TYPES:
                    return _RIFF_TYPES[riff_type]
            # ZIP-based Office formats (.docx, .xlsx, .pptx, etc.)
            # Magic bytes say "application/zip" but the real type comes
            # from the file extension.
            if mime == "application/zip":
                ext_mime, _ = mimetypes.guess_type(filename)
                if ext_mime:
                    return ext_mime
            return mime

    # 2. Extension-based guess
    mime_type, encoding = mimetypes.guess_type(filename)
    if mime_type:
        return mime_type

    # 3. Final fallback
    return "application/octet-stream"


# ═══════════════════════════════════════════════════════════════════
# Content hashing
# ═══════════════════════════════════════════════════════════════════


def compute_content_hash(file_path: Path, algorithm: str = "sha256") -> str:
    """Compute the content hash of *file_path*.

    Args:
        file_path: Absolute path to the file.
        algorithm: Hash algorithm name (default ``"sha256"``).

    Returns:
        Hex-encoded digest string.
    """
    h = hashlib.new(algorithm)
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(64 * 1024)  # 64 KB chunks
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def compute_content_hash_bytes(data: bytes, algorithm: str = "sha256") -> str:
    """Compute the content hash of *data* bytes."""
    return hashlib.new(algorithm, data).hexdigest()


# ═══════════════════════════════════════════════════════════════════
# Staging operations
# ═══════════════════════════════════════════════════════════════════


def _stage_path_for(backend: StorageBackend, original_filename: str) -> Path:
    """Build a unique staging path under the backend's storage root.

    Format: ``<backend.storage_root>/staging/<timestamp_ms>_<sanitized_name>``

    The staging subdirectory name is read from config but the root
    comes from the backend, so tests using a custom backend automatically
    get the correct path.
    """
    safe_name = sanitize_filename(original_filename)
    ts = int(time.time() * 1000)
    pid = os.getpid()
    unique_suffix = f"{ts}_{pid}"
    stem, dot, ext = safe_name.rpartition(".")
    if dot:
        staged_name = f"{stem}_{unique_suffix}.{ext}"
    else:
        staged_name = f"{safe_name}_{unique_suffix}"

    settings = get_settings()
    staging_dir = backend.storage_root / settings.staging_subdir
    return staging_dir / staged_name


def stage_file(
    *,
    file_content: bytes,
    original_filename: str,
    backend: StorageBackend | None = None,
) -> StagedFileInfo:
    """Write *file_content* to a staging path and return metadata.

    Performs:
    1. Filename sanitisation.
    2. MIME type detection from magic bytes + extension.
    3. Content hash computation (SHA-256).
    4. Write to the staging directory via the configured backend.

    Args:
        file_content: Raw bytes of the uploaded file.
        original_filename: The original (unsanitised) filename.
        backend: The storage backend to use.  If ``None``, uses the
            configured singleton via :func:`get_backend`.

    Returns:
        :class:`StagedFileInfo` describing the staged file.

    Raises:
        ValueError: If *file_content* is empty or the filename is unsafe.
    """
    if not file_content:
        raise ValueError("file_content must not be empty")

    if not original_filename or not original_filename.strip():
        raise ValueError("original_filename must not be empty")

    b = backend or get_backend()
    safe_name = sanitize_filename(original_filename)

    # Determine staging path
    staging_path = _stage_path_for(b, safe_name)

    # MIME detection (use first 2048 bytes)
    content_head = file_content[:2048]
    media_type = detect_mime_type(original_filename, content_head)

    # Content hash
    content_hash = compute_content_hash_bytes(file_content)

    # Write to staging
    b.write_file(staging_path, file_content)

    # Generate staging token (opaque reference)
    staging_token = f"{staging_path.name}:{content_hash[:12]}"

    return StagedFileInfo(
        staging_path=str(staging_path),
        original_filename=safe_name,
        content_hash=content_hash,
        size_bytes=len(file_content),
        media_type=media_type,
        staging_token=staging_token,
    )


def stage_file_from_stream(
    *,
    stream: BinaryIO,
    original_filename: str,
    backend: StorageBackend | None = None,
    chunk_size: int = 64 * 1024,
) -> StagedFileInfo:
    """Stage an uploaded file from a binary stream, hashing on the fly.

    This is the preferred method for large files, as it avoids loading
    the entire file into memory.  The content is read in *chunk_size*
    blocks, written to disk, and the hash is computed incrementally.

    Args:
        stream: A binary file-like object (e.g. ``UploadFile.file``).
        original_filename: The original unsanitised filename.
        backend: Storage backend (uses singleton if ``None``).
        chunk_size: Read/write chunk size in bytes.

    Returns:
        :class:`StagedFileInfo`.
    """
    if not original_filename or not original_filename.strip():
        raise ValueError("original_filename must not be empty")

    b = backend or get_backend()
    safe_name = sanitize_filename(original_filename)
    staging_path = _stage_path_for(b, safe_name)

    settings = get_settings()
    staging_dir = b.storage_root / settings.staging_subdir
    b.ensure_directory(staging_dir)

    hasher = hashlib.sha256()
    total_bytes = 0
    content_head = b""

    with open(staging_path, "wb") as dst:
        while True:
            chunk = stream.read(chunk_size)
            if not chunk:
                break
            if len(chunk) > settings.max_upload_size_bytes - total_bytes:
                # Remove partial file
                try:
                    os.unlink(staging_path)
                except OSError:
                    pass
                raise ValueError(
                    f"File exceeds maximum upload size of "
                    f"{settings.max_upload_size_bytes} bytes"
                )
            if not content_head:
                content_head = chunk[:2048]
            hasher.update(chunk)
            dst.write(chunk)
            total_bytes += len(chunk)

    if total_bytes == 0:
        try:
            os.unlink(staging_path)
        except OSError:
            pass
        raise ValueError("Uploaded file is empty")

    content_hash = hasher.hexdigest()
    media_type = detect_mime_type(original_filename, content_head)
    staging_token = f"{staging_path.name}:{content_hash[:12]}"

    return StagedFileInfo(
        staging_path=str(staging_path),
        original_filename=safe_name,
        content_hash=content_hash,
        size_bytes=total_bytes,
        media_type=media_type,
        staging_token=staging_token,
    )


def _cleanup_staging_dir(
    backend: StorageBackend | None = None,
    max_age_seconds: int = 86400,  # 24 hours
) -> int:
    """Remove staged files older than *max_age_seconds*.

    Returns the number of files removed.
    """
    b = backend or get_backend()
    settings = get_settings()
    staging_root = b.storage_root / settings.staging_subdir

    if not staging_root.is_dir():
        return 0

    now = time.time()
    removed = 0

    for entry in staging_root.iterdir():
        if entry.is_file():
            try:
                age = now - entry.stat().st_mtime
                if age > max_age_seconds:
                    b.delete_file(entry)
                    removed += 1
            except OSError:
                pass

    return removed


def resolve_staged_file(
    staging_token: str,
    backend: StorageBackend | None = None,
) -> Path | None:
    """Resolve a *staging_token* to an absolute file path.

    The staging token format is ``<filename>:<hash_prefix>``.
    Returns ``None`` if the file no longer exists.
    """
    b = backend or get_backend()
    settings = get_settings()
    staging_root = b.storage_root / settings.staging_subdir
    # Token format: <staged_name>:<hash_prefix>
    if ":" not in staging_token:
        return None
    staged_name = staging_token.split(":")[0]
    safe_name = sanitize_filename(staged_name)
    path = staging_root / safe_name
    if b.file_exists(path):
        return path
    return None
