"""Batch file encryption/decryption utilities.

Provides functions to encrypt or decrypt entire directories of files
using AES-256-GCM (via file_encrypt module).
"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from mneme.security.file_encrypt import decrypt_file, encrypt_file, is_encrypted

logger = logging.getLogger("mneme.security.batch")

# Files to skip during batch operations
_SKIP_NAMES = frozenset({".gitkeep", ".gitignore", ".DS_Store"})


@dataclass
class BatchResult:
    """Result of a batch encryption/decryption operation."""

    processed: int = 0
    skipped: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)

    @property
    def success_count(self) -> int:
        return self.processed

    @property
    def error_count(self) -> int:
        return len(self.errors)

    @property
    def total(self) -> int:
        return self.processed + self.skipped + self.error_count


def _should_skip(file_path: Path) -> bool:
    """Check if a file should be skipped during batch operations.

    Only skips specific known non-data files (git metadata, macOS metadata).
    Hidden files like .env or .secrets are NOT skipped — they may contain
    sensitive data that should be encrypted.
    """
    name = file_path.name
    if name in _SKIP_NAMES:
        return True
    return False


def _atomic_write(file_path: Path, data: bytes) -> None:
    """Write data to a file atomically using temp-file-then-rename.

    This prevents data loss if the process crashes mid-write.
    Preserves the original file's permissions.
    """
    fd, tmp_path = tempfile.mkstemp(
        dir=str(file_path.parent),
        prefix=".encrypt_tmp_",
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)

        # Preserve original file permissions
        try:
            original_mode = file_path.stat().st_mode
            os.chmod(tmp_path, original_mode)
        except OSError:
            pass  # Best-effort; temp file keeps default 0o600

        os.replace(tmp_path, str(file_path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def batch_encrypt(
    directory: str | Path,
    key: bytes,
    *,
    recursive: bool = True,
) -> BatchResult:
    """Encrypt all files in a directory.

    Uses atomic writes (temp file + rename) to prevent data loss on failure.

    Args:
        directory: Directory containing files to encrypt.
        key: 256-bit AES key (32 bytes).
        recursive: Process subdirectories recursively.

    Returns:
        BatchResult with operation statistics.

    Raises:
        ValueError: If key is not 32 bytes.
    """
    if len(key) != 32:
        raise ValueError("Key must be 32 bytes (256 bits)")

    dir_path = Path(directory)
    if not dir_path.is_dir():
        raise ValueError(f"Not a directory: {dir_path}")

    result = BatchResult()

    for file_path in _iter_files(dir_path, recursive):
        if _should_skip(file_path):
            continue

        try:
            content = file_path.read_bytes()

            # Skip empty files
            if not content:
                logger.debug("Skipping empty file: %s", file_path.name)
                result.skipped += 1
                continue

            # Skip already encrypted files
            if is_encrypted(content):
                logger.debug("Already encrypted, skipping: %s", file_path.name)
                result.skipped += 1
                continue

            # Encrypt and write atomically
            encrypted, _ = encrypt_file(content, key)
            _atomic_write(file_path, encrypted)

            result.processed += 1
            logger.info("Encrypted: %s (%d -> %d bytes)", file_path.name, len(content), len(encrypted))

        except Exception as e:
            result.errors.append((str(file_path), str(e)))
            logger.error("Failed to encrypt %s: %s", file_path.name, e)

    return result


def batch_decrypt(
    directory: str | Path,
    key: bytes,
    *,
    recursive: bool = True,
) -> BatchResult:
    """Decrypt all encrypted files in a directory.

    Only processes files with the MNME magic header (encrypted marker).
    Uses atomic writes (temp file + rename) to prevent data loss on failure.

    Args:
        directory: Directory containing encrypted files.
        key: 256-bit AES key (32 bytes).
        recursive: Process subdirectories recursively.

    Returns:
        BatchResult with operation statistics.

    Raises:
        ValueError: If key is not 32 bytes.
    """
    if len(key) != 32:
        raise ValueError("Key must be 32 bytes (256 bits)")

    dir_path = Path(directory)
    if not dir_path.is_dir():
        raise ValueError(f"Not a directory: {dir_path}")

    result = BatchResult()

    for file_path in _iter_files(dir_path, recursive):
        if _should_skip(file_path):
            continue

        try:
            content = file_path.read_bytes()

            # Only decrypt files with magic header
            if not is_encrypted(content):
                result.skipped += 1
                continue

            # Decrypt and write atomically
            decrypted = decrypt_file(content, key)
            _atomic_write(file_path, decrypted)

            result.processed += 1
            logger.info("Decrypted: %s (%d -> %d bytes)", file_path.name, len(content), len(decrypted))

        except Exception as e:
            result.errors.append((str(file_path), str(e)))
            logger.error("Failed to decrypt %s: %s", file_path.name, e)

    return result


def encrypt_directory_tree(
    root: str | Path,
    key: bytes,
    *,
    public_dir: str = "public",
    private_dir: str = "private",
) -> dict[str, BatchResult]:
    """Encrypt all files in the private directory of a storage tree.

    Structure:
        root/
        ├── public/   (skipped — not encrypted)
        └── private/  (all files encrypted)

    Args:
        root: Root of the directory tree.
        key: 256-bit AES key.
        public_dir: Name of the public directory (skipped).
        private_dir: Name of the private directory (encrypted).

    Returns:
        Dict with "private" key containing the BatchResult.
    """
    root_path = Path(root)
    results: dict[str, BatchResult] = {}

    private_path = root_path / private_dir
    if private_path.is_dir():
        results["private"] = batch_encrypt(private_path, key)
        logger.info(
            "Private directory: %d encrypted, %d skipped, %d errors",
            results["private"].processed,
            results["private"].skipped,
            results["private"].error_count,
        )
    else:
        logger.warning("Private directory not found: %s", private_path)
        results["private"] = BatchResult()

    return results


def _iter_files(directory: Path, recursive: bool):
    """Yield regular files (not symlinks, not special files) from a directory.

    Uses os.walk for reliable traversal that handles hidden directories
    and avoids symlink loops.
    """
    if recursive:
        for dirpath, dirnames, filenames in os.walk(directory, followlinks=False):
            # Skip hidden directories
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fname in filenames:
                if fname.startswith("."):
                    continue
                fpath = Path(dirpath) / fname
                # Skip symlinks and non-regular files (FIFOs, sockets, etc.)
                try:
                    if fpath.is_symlink() or not fpath.is_file():
                        continue
                except OSError:
                    continue
                yield fpath
    else:
        try:
            for entry in directory.iterdir():
                if entry.is_file() and not entry.is_symlink() and not entry.name.startswith("."):
                    yield entry
        except OSError as e:
            logger.warning("Error listing directory %s: %s", directory, e)
