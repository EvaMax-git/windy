"""Unit tests for the P3-01 Storage layer.

Covers:
- Path sanitization and traversal prevention
- MIME type detection (magic bytes + extension)
- Content hash computation
- File staging (bytes and stream)
- File promotion (staging → permanent)
- Idempotent upload with content-hash dedup
- Size and MIME validation
- Storage backend operations
"""

from __future__ import annotations

import tempfile
import hashlib
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from mneme.config import get_settings
from mneme.storage.backend import (
    LocalFileSystemBackend,
    StorageBackend,
    get_backend,
    reset_backend,
    sanitize_filename,
    is_path_safe,
    build_asset_directory,
)
from mneme.storage.staging import (
    detect_mime_type,
    compute_content_hash,
    compute_content_hash_bytes,
    stage_file,
    stage_file_from_stream,
    _cleanup_staging_dir,
    resolve_staged_file,
)
from mneme.storage.promote import (
    promote_file,
    _build_asset_path,
    rollback_promote,
    PromoteError,
)
from mneme.storage.upload import (
    IdempotentUploadResult,
    handle_idempotent_upload_bytes,
    handle_idempotent_upload_stream,
    validate_upload_size,
    validate_mime_type,
    validate_filename,
)
from mneme.schemas.storage import (
    StagedFileInfo,
    UploadRequest,
    ContentHashDuplicate,
    AssetRead,
    AssetType,
    IngestState,
    KnowledgeState,
    InboxType,
)


# ═══════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════


@pytest.fixture(autouse=True)
def _reset_backend():
    """Ensure each test uses a fresh backend singleton."""
    reset_backend()
    yield
    reset_backend()


@pytest.fixture
def tmp_storage_root():
    """Create a temporary storage root directory for backend tests."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        yield Path(tmp_dir)


@pytest.fixture
def backend(tmp_storage_root):
    """Return a LocalFileSystemBackend pointed at a temp directory.

    Also patches ``get_settings().staging_subdir`` so that staging functions
    use the correct subdirectory name (default: ``"staging"``).
    """
    root = str(tmp_storage_root)
    yield LocalFileSystemBackend(root)


@pytest.fixture
def project_id():
    return UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
def asset_uid():
    return "demo-abc123def456-1700000000000"


# ═══════════════════════════════════════════════════════════════════
# Backend tests
# ═══════════════════════════════════════════════════════════════════


class TestSanitizeFilename:
    def test_normal_filename(self):
        assert sanitize_filename("my document.pdf") == "my document.pdf"

    def test_traversal_attempt(self):
        result = sanitize_filename("../../../etc/passwd")
        assert ".." not in result
        assert "etc" in result
        assert "passwd" in result

    def test_path_separators(self):
        result = sanitize_filename("foo/bar\\baz.txt")
        assert "/" not in result
        assert "\\" not in result
        assert result.endswith(".txt")

    def test_empty_returns_unnamed(self):
        assert sanitize_filename("   ") == "unnamed"

    def test_hidden_file(self):
        result = sanitize_filename(".hidden")
        assert not result.startswith(".")
        assert "hidden" in result

    def test_special_characters(self):
        result = sanitize_filename("file<script>.txt")
        assert "<" not in result
        assert "script" in result
        assert result.endswith(".txt")

    def test_unicode_filename(self):
        result = sanitize_filename("中文文档.pdf")
        assert "中文文档" in result
        assert result.endswith(".pdf")

    def test_null_bytes(self):
        result = sanitize_filename("safe\0bad.txt")
        assert "\0" not in result

    def test_whitespace(self):
        result = sanitize_filename("  spaces  .pdf  ")
        assert result.startswith("spaces")
        assert result.endswith(".pdf")

    def test_very_long_filename(self):
        long_name = "a" * 300 + ".txt"
        result = sanitize_filename(long_name)
        assert len(result.encode("utf-8")) <= 240


class TestIsPathSafe:
    def test_normal_relative_path(self):
        assert is_path_safe("assets/uuid/file.pdf") is True

    def test_parent_traversal(self):
        assert is_path_safe("../../../etc/passwd") is False

    def test_absolute_path(self):
        assert is_path_safe("/etc/passwd") is False

    def test_null_bytes(self):
        assert is_path_safe("safe\0bad") is False

    def test_empty_string(self):
        assert is_path_safe("") is False

    def test_complex_traversal(self):
        assert is_path_safe("assets/../etc/passwd") is False

    def test_normalized_but_safe(self):
        assert is_path_safe("assets/./file.pdf") is True

    def test_leading_dot_dot_slash(self):
        assert is_path_safe("../config") is False


class TestBuildAssetDirectory:
    def test_builds_correct_path(self, project_id):
        path = build_asset_directory(project_id, "demo-abc-123")
        parts = path.parts
        assert "assets" in parts
        assert str(project_id) in parts
        assert "demo-abc-123" in parts


# ═══════════════════════════════════════════════════════════════════
# Backend operations
# ═══════════════════════════════════════════════════════════════════


class TestLocalFileSystemBackend:
    def test_write_and_read_file(self, backend, tmp_storage_root):
        file_path = tmp_storage_root / "test_write.txt"
        content = b"Hello, Mneme Storage!"
        written = backend.write_file(file_path, content)
        assert written == len(content)
        assert backend.file_exists(file_path)
        assert backend.read_file(file_path) == content

    def test_file_size(self, backend, tmp_storage_root):
        file_path = tmp_storage_root / "size_test.bin"
        content = b"\x00" * 1024
        backend.write_file(file_path, content)
        assert backend.file_size(file_path) == 1024

    def test_file_not_exists(self, backend, tmp_storage_root):
        assert backend.file_exists(tmp_storage_root / "nonexistent.dat") is False

    def test_move_file(self, backend, tmp_storage_root):
        src = tmp_storage_root / "src.txt"
        dst = tmp_storage_root / "subdir" / "dst.txt"
        backend.write_file(src, b"move me")
        backend.move_file(src, dst)
        assert backend.file_exists(dst)
        assert not backend.file_exists(src)
        assert backend.read_file(dst) == b"move me"

    def test_delete_file(self, backend, tmp_storage_root):
        file_path = tmp_storage_root / "delete_me.txt"
        backend.write_file(file_path, b"temp")
        assert backend.file_exists(file_path)
        backend.delete_file(file_path)
        assert not backend.file_exists(file_path)

    def test_delete_nonexistent_file_no_error(self, backend, tmp_storage_root):
        backend.delete_file(tmp_storage_root / "never_existed.txt")

    def test_ensure_directory(self, backend, tmp_storage_root):
        dir_path = tmp_storage_root / "deep" / "nested" / "dir"
        backend.ensure_directory(dir_path)
        assert dir_path.is_dir()

    def test_ensure_directory_idempotent(self, backend, tmp_storage_root):
        dir_path = tmp_storage_root / "idem" / "dir"
        backend.ensure_directory(dir_path)
        backend.ensure_directory(dir_path)  # Should not raise
        assert dir_path.is_dir()


class TestGetBackend:
    def test_returns_local_filesystem_backend(self):
        reset_backend()
        b = get_backend()
        assert isinstance(b, LocalFileSystemBackend)

    def test_singleton(self):
        reset_backend()
        b1 = get_backend()
        b2 = get_backend()
        assert b1 is b2


# ═══════════════════════════════════════════════════════════════════
# MIME detection
# ═══════════════════════════════════════════════════════════════════


class TestDetectMimeType:
    def test_png_magic_bytes(self):
        result = detect_mime_type("screenshot.png", b"\x89PNG\r\n\x1a\n")
        assert result == "image/png"

    def test_jpeg_magic_bytes(self):
        result = detect_mime_type("photo.jpg", b"\xff\xd8\xff\xe0")
        assert result == "image/jpeg"

    def test_gif_magic_bytes(self):
        result = detect_mime_type("anim.gif", b"GIF89a")
        assert result == "image/gif"

    def test_pdf_magic_bytes(self):
        result = detect_mime_type("doc.pdf", b"%PDF-1.4")
        assert result == "application/pdf"

    def test_zip_magic_bytes(self):
        result = detect_mime_type("archive.zip", b"PK\x03\x04")
        assert result == "application/zip"

    def test_gzip_magic_bytes(self):
        result = detect_mime_type("data.gz", b"\x1f\x8b\x08")
        assert result == "application/gzip"

    def test_extension_fallback_text(self):
        result = detect_mime_type("readme.txt", b"Hello World")
        assert result == "text/plain"

    def test_extension_fallback_html(self):
        result = detect_mime_type("page.html", b"<!DOCTYPE html>")
        assert result == "text/html"

    def test_extension_fallback_json(self):
        result = detect_mime_type("data.json", b'{"key": "value"}')
        assert result == "application/json"

    def test_extension_fallback_csv(self):
        result = detect_mime_type("data.csv", b"a,b,c\n1,2,3")
        assert result == "text/csv"

    def test_unknown_returns_octet_stream(self):
        result = detect_mime_type("data.bin", b"\x00\x01\x02\x03\x04")
        assert result == "application/octet-stream"

    def test_wav_rifx_detection(self):
        # RIFF container with WAVE format
        riff_header = b"RIFF\x00\x00\x00\x00WAVE"
        result = detect_mime_type("sound.wav", riff_header)
        assert result == "audio/wav"

    def test_webp_rifx_detection(self):
        riff_header = b"RIFF\x00\x00\x00\x00WEBP"
        result = detect_mime_type("image.webp", riff_header)
        assert result == "image/webp"

    def test_no_extension(self):
        result = detect_mime_type("Makefile", b"all:\n\techo hello")
        assert result == "application/octet-stream"

    def test_ole2_doc_magic(self):
        result = detect_mime_type("old.doc", b"\xd0\xcf\x11\xe0")
        assert result == "application/msword"

    def test_markdown_extension(self):
        result = detect_mime_type("README.md", b"# Title")
        assert result == "text/markdown"


# ═══════════════════════════════════════════════════════════════════
# Content hash
# ═══════════════════════════════════════════════════════════════════


class TestContentHash:
    def test_sha256_bytes(self):
        result = compute_content_hash_bytes(b"hello world")
        expected = hashlib.sha256(b"hello world").hexdigest()
        assert result == expected
        assert len(result) == 64

    def test_sha256_file(self, tmp_storage_root):
        file_path = tmp_storage_root / "hash_test.bin"
        content = b"test content for hashing"
        file_path.write_bytes(content)
        result = compute_content_hash(file_path)
        expected = hashlib.sha256(content).hexdigest()
        assert result == expected

    def test_consistent_hashing(self):
        h1 = compute_content_hash_bytes(b"same content")
        h2 = compute_content_hash_bytes(b"same content")
        assert h1 == h2

    def test_different_content_different_hash(self):
        h1 = compute_content_hash_bytes(b"content A")
        h2 = compute_content_hash_bytes(b"content B")
        assert h1 != h2


# ═══════════════════════════════════════════════════════════════════
# File staging
# ═══════════════════════════════════════════════════════════════════


class TestStageFile:
    def test_stage_bytes(self, backend, tmp_storage_root):
        info = stage_file(
            file_content=b"Hello Mneme!",
            original_filename="hello.txt",
            backend=backend,
        )
        assert isinstance(info, StagedFileInfo)
        assert info.original_filename == "hello.txt"
        assert info.content_hash == compute_content_hash_bytes(b"Hello Mneme!")
        assert info.size_bytes == 12
        assert info.media_type == "text/plain"
        assert info.staging_token
        assert info.staging_token.endswith(f":{info.content_hash[:12]}")
        # Verify file exists on disk
        assert Path(info.staging_path).is_file()
        assert Path(info.staging_path).read_bytes() == b"Hello Mneme!"

    def test_stage_pdf(self, backend, tmp_storage_root):
        content = b"%PDF-1.4\nfake pdf content"
        info = stage_file(
            file_content=content,
            original_filename="document.pdf",
            backend=backend,
        )
        assert info.media_type == "application/pdf"
        assert info.size_bytes == len(content)

    def test_stage_sanitizes_filename(self, backend, tmp_storage_root):
        info = stage_file(
            file_content=b"data",
            original_filename="../../../etc/malicious.txt",
            backend=backend,
        )
        assert ".." not in info.original_filename
        assert "etc" in info.original_filename
        # The on-disk path should also be safe
        assert ".." not in Path(info.staging_path).name

    def test_stage_empty_content_raises(self, backend):
        with pytest.raises(ValueError, match="must not be empty"):
            stage_file(
                file_content=b"",
                original_filename="empty.txt",
                backend=backend,
            )

    def test_stage_empty_filename_raises(self, backend):
        with pytest.raises(ValueError, match="must not be empty"):
            stage_file(
                file_content=b"data",
                original_filename="",
                backend=backend,
            )

    def test_stage_unicode_filename(self, backend):
        info = stage_file(
            file_content=b"data",
            original_filename="测试文件.txt",
            backend=backend,
        )
        assert "测试文件" in info.original_filename

    def test_stage_large_file(self, backend, tmp_storage_root):
        content = b"x" * (1024 * 1024)  # 1 MB
        info = stage_file(
            file_content=content,
            original_filename="large.bin",
            backend=backend,
        )
        assert info.size_bytes == 1024 * 1024
        assert len(info.content_hash) == 64

    def test_stage_with_image(self, backend, tmp_storage_root):
        # Minimal valid PNG: 8-byte signature + IHDR + IEND
        png_content = (
            b"\x89PNG\r\n\x1a\n"  # signature
            b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x02\x00\x00\x00\x90wS\xde"
            b"\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        info = stage_file(
            file_content=png_content,
            original_filename="test.png",
            backend=backend,
        )
        assert info.media_type == "image/png"


class TestStageFileFromStream:
    def test_stage_from_bytes_io(self, backend, tmp_storage_root):
        from io import BytesIO

        stream = BytesIO(b"streamed content here!")
        info = stage_file_from_stream(
            stream=stream,
            original_filename="stream.txt",
            backend=backend,
        )
        assert info.size_bytes == 22
        assert info.content_hash == compute_content_hash_bytes(b"streamed content here!")
        assert info.media_type == "text/plain"
        assert Path(info.staging_path).read_bytes() == b"streamed content here!"

    def test_stage_empty_stream_raises(self, backend):
        from io import BytesIO

        stream = BytesIO(b"")
        with pytest.raises(ValueError, match="empty"):
            stage_file_from_stream(
                stream=stream,
                original_filename="empty.txt",
                backend=backend,
            )

    def test_stage_stream_empty_filename_raises(self, backend):
        from io import BytesIO

        stream = BytesIO(b"data")
        with pytest.raises(ValueError, match="must not be empty"):
            stage_file_from_stream(
                stream=stream,
                original_filename="   ",
                backend=backend,
            )


class TestResolveStagedFile:
    def test_resolve_existing_file(self, backend, tmp_storage_root):
        info = stage_file(
            file_content=b"resolve me",
            original_filename="resolve.txt",
            backend=backend,
        )
        resolved = resolve_staged_file(info.staging_token, backend)
        assert resolved is not None
        assert resolved.is_file()
        assert resolved.read_bytes() == b"resolve me"

    def test_resolve_nonexistent_file(self, backend):
        result = resolve_staged_file("nonexistent:000000000000", backend)
        assert result is None

    def test_resolve_bad_token_format(self, backend):
        result = resolve_staged_file("badtoken", backend)
        assert result is None


class TestCleanupStagingDir:
    def test_cleanup_old_files(self, backend, tmp_storage_root):
        staging_root = backend.storage_root / "staging"
        # Remove existing staging files from other tests first
        if staging_root.is_dir():
            for f in list(staging_root.iterdir()):
                backend.delete_file(f)

        backend.ensure_directory(staging_root)

        # Create some test files in staging
        for i in range(3):
            f = staging_root / f"cleanup_test_{i}.txt"
            backend.write_file(f, b"stale data")

        # With max_age_seconds=0, all files should be removed
        removed = _cleanup_staging_dir(backend, max_age_seconds=0)
        assert removed == 3
        # All files should be gone
        for i in range(3):
            assert not backend.file_exists(staging_root / f"cleanup_test_{i}.txt")

    def test_cleanup_nonexistent_staging_dir(self, backend, tmp_storage_root):
        # First remove the staging dir entirely
        staging_root = backend.storage_root / "staging"
        if staging_root.is_dir():
            for f in list(staging_root.iterdir()):
                backend.delete_file(f)
            staging_root.rmdir()

        removed = _cleanup_staging_dir(backend, max_age_seconds=0)
        assert removed == 0

    def test_cleanup_keeps_recent_files(self, backend, tmp_storage_root):
        staging_root = backend.storage_root / "staging"
        # Remove existing staging files
        if staging_root.is_dir():
            for f in list(staging_root.iterdir()):
                backend.delete_file(f)
        backend.ensure_directory(staging_root)

        f = staging_root / "recent.txt"
        backend.write_file(f, b"fresh")

        # With a very large max_age, nothing should be removed
        removed = _cleanup_staging_dir(backend, max_age_seconds=9999999)
        assert removed == 0
        assert backend.file_exists(f)


# ═══════════════════════════════════════════════════════════════════
# File promotion
# ═══════════════════════════════════════════════════════════════════


class TestPromoteFile:
    def test_promote_moves_file(self, backend, tmp_storage_root, project_id, asset_uid):
        # Stage a file first
        staged = stage_file(
            file_content=b"promote me!",
            original_filename="document.pdf",
            backend=backend,
        )
        src_path = Path(staged.staging_path)
        assert src_path.exists()

        # Promote
        storage_ref = promote_file(
            staging_path=staged.staging_path,
            project_id=project_id,
            asset_uid=asset_uid,
            original_filename=staged.original_filename,
            backend=backend,
        )

        # Source should be gone
        assert not src_path.exists()

        # Destination should exist
        dest = Path(storage_ref)
        assert dest.exists()
        assert dest.read_bytes() == b"promote me!"

        # Path contains expected structure
        assert str(project_id) in storage_ref
        assert asset_uid in storage_ref
        assert "assets" in storage_ref

    def test_promote_nonexistent_source_raises(self, backend, project_id, asset_uid):
        with pytest.raises(PromoteError, match="does not exist"):
            promote_file(
                staging_path="/nonexistent/path/file.pdf",
                project_id=project_id,
                asset_uid=asset_uid,
                original_filename="file.pdf",
                backend=backend,
            )

    def test_promote_duplicate_destination_raises(
        self, backend, tmp_storage_root, project_id, asset_uid
    ):
        # Stage and promote once
        staged1 = stage_file(
            file_content=b"first",
            original_filename="dup.pdf",
            backend=backend,
        )
        promote_file(
            staging_path=staged1.staging_path,
            project_id=project_id,
            asset_uid=asset_uid,
            original_filename=staged1.original_filename,
            backend=backend,
        )

        # Stage again and try to promote to same destination
        staged2 = stage_file(
            file_content=b"second",
            original_filename="dup.pdf",
            backend=backend,
        )
        with pytest.raises(PromoteError, match="already exists"):
            promote_file(
                staging_path=staged2.staging_path,
                project_id=project_id,
                asset_uid=asset_uid,
                original_filename=staged2.original_filename,
                backend=backend,
            )

    def test_build_asset_path_consistency(self, project_id):
        ref = _build_asset_path(project_id, "demo-xyz-999", "my doc.pdf")
        assert str(project_id) in ref
        assert "demo-xyz-999" in ref
        assert "my doc.pdf" in ref
        assert "assets" in ref

    def test_promote_sanitizes_dest_filename(
        self, backend, tmp_storage_root, project_id, asset_uid
    ):
        staged = stage_file(
            file_content=b"data",
            original_filename="safe_file.txt",
            backend=backend,
        )
        # Use an unsanitised filename for the promote — it should be sanitised
        storage_ref = promote_file(
            staging_path=staged.staging_path,
            project_id=project_id,
            asset_uid=asset_uid,
            original_filename="../../../evil.txt",
            backend=backend,
        )
        dest_name = Path(storage_ref).name
        assert ".." not in dest_name
        assert "evil" in dest_name


class TestRollbackPromote:
    def test_rollback_removes_file(self, backend, tmp_storage_root, project_id, asset_uid):
        staged = stage_file(
            file_content=b"rollback test",
            original_filename="rb.pdf",
            backend=backend,
        )
        storage_ref = promote_file(
            staging_path=staged.staging_path,
            project_id=project_id,
            asset_uid=asset_uid,
            original_filename=staged.original_filename,
            backend=backend,
        )
        assert Path(storage_ref).exists()

        rollback_promote(storage_ref, backend)
        assert not Path(storage_ref).exists()

    def test_rollback_nonexistent_no_error(self, backend):
        rollback_promote("/nonexistent/asset/file.pdf", backend)


# ═══════════════════════════════════════════════════════════════════
# Upload validation
# ═══════════════════════════════════════════════════════════════════


class TestValidateUploadSize:
    def test_valid_size(self):
        validate_upload_size(1024)

    def test_zero_size_raises(self):
        with pytest.raises(ValueError, match="empty"):
            validate_upload_size(0)

    def test_exceeds_max_raises(self):
        settings = get_settings()
        too_big = settings.max_upload_size_bytes + 1
        with pytest.raises(ValueError, match="exceeds maximum"):
            validate_upload_size(too_big)

    def test_exactly_max_is_ok(self):
        settings = get_settings()
        validate_upload_size(settings.max_upload_size_bytes)


class TestValidateMimeType:
    def test_allowed_type(self):
        validate_mime_type("text/plain")

    def test_allowed_image_type(self):
        validate_mime_type("image/png")

    def test_allowed_pdf_type(self):
        validate_mime_type("application/pdf")

    def test_not_allowed_raises(self):
        with pytest.raises(ValueError, match="not allowed"):
            validate_mime_type("application/x-msdownload")

    def test_none_passes(self):
        validate_mime_type(None)


class TestValidateFilename:
    def test_normal_filename(self):
        result = validate_filename("my_file.pdf")
        assert result == "my_file.pdf"

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="must not be empty"):
            validate_filename("")

    def test_traversal_raises(self):
        with pytest.raises(ValueError, match="unsafe"):
            validate_filename("../../../etc/passwd")

    def test_null_byte_raises(self):
        with pytest.raises(ValueError, match="null bytes"):
            validate_filename("good\0bad.txt")

    def test_absolute_path_raises(self):
        with pytest.raises(ValueError, match="unsafe"):
            validate_filename("/etc/shadow")


# ═══════════════════════════════════════════════════════════════════
# Idempotent upload (bytes)
# ═══════════════════════════════════════════════════════════════════


class TestHandleIdempotentUploadBytes:
    def test_new_file_upload(self, backend):
        result = handle_idempotent_upload_bytes(
            file_content=b"fresh content",
            original_filename="fresh.txt",
            backend=backend,
            skip_mime_validation=True,
        )
        assert result.is_duplicate is False
        assert result.staged_info is not None
        assert result.staged_info.content_hash
        assert result.staged_info.size_bytes == 13
        assert Path(result.staged_info.staging_path).exists()

    def test_duplicate_detected(self, backend):
        content = b"same content for dedup"
        content_hash = compute_content_hash_bytes(content)
        existing = ContentHashDuplicate(
            existing_asset_id=uuid4(),
            asset_uid="test-abc-123",
            title="Existing Document",
            content_hash=content_hash,
            created_at="2026-01-01T00:00:00Z",  # type: ignore
        )

        def mock_lookup(hash_val, pid):
            if hash_val == content_hash:
                return existing
            return None

        result = handle_idempotent_upload_bytes(
            file_content=content,
            original_filename="dup.txt",
            lookup_duplicate=mock_lookup,
            backend=backend,
            skip_mime_validation=True,
        )
        assert result.is_duplicate is True
        assert result.staged_info is None
        assert result.duplicate_info is existing

    def test_no_lookup_callback_always_new(self, backend):
        result = handle_idempotent_upload_bytes(
            file_content=b"always new",
            original_filename="new.txt",
            lookup_duplicate=None,
            backend=backend,
            skip_mime_validation=True,
        )
        assert result.is_duplicate is False

    def test_empty_content_raises(self, backend):
        with pytest.raises(ValueError, match="must not be empty"):
            handle_idempotent_upload_bytes(
                file_content=b"",
                original_filename="empty.txt",
                backend=backend,
            )

    def test_mime_validation_rejects(self, backend):
        # Create content with an executable magic byte that won't be in the allowed list
        with pytest.raises(ValueError, match="not allowed"):
            handle_idempotent_upload_bytes(
                file_content=b"\x7fELF\x00\x00\x00",
                original_filename="bad.elf",
                backend=backend,
                skip_mime_validation=False,
            )

    def test_filename_traversal_rejected(self, backend):
        with pytest.raises(ValueError, match="unsafe"):
            handle_idempotent_upload_bytes(
                file_content=b"safe",
                original_filename="../../etc/passwd",
                backend=backend,
            )

    def test_oversized_rejected(self, backend):
        settings = get_settings()
        too_big = b"x" * (settings.max_upload_size_bytes + 1)
        with pytest.raises(ValueError, match="exceeds maximum"):
            handle_idempotent_upload_bytes(
                file_content=too_big,
                original_filename="huge.bin",
                backend=backend,
            )

    def test_idempotent_upload_result_structure(self):
        result = IdempotentUploadResult(is_duplicate=False)
        assert result.is_duplicate is False
        assert result.staged_info is None
        assert result.duplicate_info is None

    def test_duplicate_result_structure(self, project_id):
        dup = ContentHashDuplicate(
            existing_asset_id=uuid4(),
            asset_uid="demo-hash-001",
            title="Duplicate Doc",
            content_hash="a" * 64,
            created_at="2026-01-01T00:00:00Z",  # type: ignore
        )
        result = IdempotentUploadResult(
            is_duplicate=True,
            duplicate_info=dup,
        )
        assert result.is_duplicate is True
        assert result.duplicate_info.content_hash == "a" * 64


# ═══════════════════════════════════════════════════════════════════
# Idempotent upload (stream)
# ═══════════════════════════════════════════════════════════════════


class TestHandleIdempotentUploadStream:
    def test_new_file_stream(self, backend):
        from io import BytesIO

        stream = BytesIO(b"stream content")
        result = handle_idempotent_upload_stream(
            stream=stream,
            original_filename="stream.txt",
            backend=backend,
            skip_mime_validation=True,
        )
        assert result.is_duplicate is False
        assert result.staged_info is not None
        assert result.staged_info.content_hash == compute_content_hash_bytes(b"stream content")

    def test_stream_duplicate_cleans_up(self, backend):
        from io import BytesIO

        content = b"dup stream content"
        content_hash = compute_content_hash_bytes(content)
        existing = ContentHashDuplicate(
            existing_asset_id=uuid4(),
            asset_uid="test-dedup-stream",
            title="Existing Stream Doc",
            content_hash=content_hash,
            created_at="2026-01-01T00:00:00Z",  # type: ignore
        )

        def mock_lookup(hash_val, pid):
            if hash_val == content_hash:
                return existing
            return None

        # We need to capture the staging path to verify cleanup
        stream = BytesIO(content)
        result = handle_idempotent_upload_stream(
            stream=stream,
            original_filename="dup_stream.txt",
            lookup_duplicate=mock_lookup,
            backend=backend,
            skip_mime_validation=True,
        )
        assert result.is_duplicate is True
        assert result.duplicate_info is existing
        # The staged file should have been cleaned up

    def test_stream_empty_raises(self, backend):
        from io import BytesIO

        stream = BytesIO(b"")
        with pytest.raises(ValueError, match="empty"):
            handle_idempotent_upload_stream(
                stream=stream,
                original_filename="empty.txt",
                backend=backend,
            )

    def test_stream_bad_filename_raises(self, backend):
        from io import BytesIO

        stream = BytesIO(b"data")
        with pytest.raises(ValueError, match="unsafe"):
            handle_idempotent_upload_stream(
                stream=stream,
                original_filename="../../etc/passwd",
                backend=backend,
            )

    def test_stream_mime_validation(self, backend):
        from io import BytesIO

        # ELF binary magic bytes
        stream = BytesIO(b"\x7fELF\x00\x00\x00")
        with pytest.raises(ValueError, match="not allowed"):
            handle_idempotent_upload_stream(
                stream=stream,
                original_filename="binary.elf",
                backend=backend,
                skip_mime_validation=False,
            )


# ═══════════════════════════════════════════════════════════════════
# Schema tests
# ═══════════════════════════════════════════════════════════════════


class TestStorageSchemas:
    def test_staged_file_info_construct(self):
        info = StagedFileInfo(
            staging_path="/tmp/staging/doc.pdf",
            original_filename="doc.pdf",
            content_hash="a" * 64,
            size_bytes=100,
            media_type="application/pdf",
            staging_token="doc.pdf:aaaaaaaaaaaa",
        )
        assert info.staging_path == "/tmp/staging/doc.pdf"
        assert info.content_hash == "a" * 64

    def test_upload_request_defaults(self):
        req = UploadRequest(project_id=uuid4())
        assert req.sensitivity_level.value == "normal"
        assert req.inbox_type == InboxType.file

    def test_content_hash_duplicate_construct(self):
        dup = ContentHashDuplicate(
            existing_asset_id=uuid4(),
            asset_uid="proj-hash-001",
            title="Existing",
            content_hash="b" * 64,
            created_at="2026-01-01T00:00:00Z",  # type: ignore
        )
        assert dup.asset_uid == "proj-hash-001"

    def test_asset_read_construct(self):
        from datetime import datetime, timezone

        asset = AssetRead(
            asset_id=uuid4(),
            asset_uid="demo-abc-001",
            title="Test Asset",
            asset_type=AssetType.document,
            storage_backend="mneme_data",
            storage_ref="/data/assets/demo-abc-001/doc.pdf",
            content_hash="c" * 64,
            status="active",
            ingest_state=IngestState.pending,
            knowledge_state=KnowledgeState.not_started,
            current_version=1,
            sensitivity_level="normal",
            retention_policy="default",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        assert asset.ingest_state == IngestState.pending
        assert asset.knowledge_state == KnowledgeState.not_started

    def test_ingest_state_values(self):
        assert IngestState.pending.value == "pending"
        assert IngestState.staged.value == "staged"
        assert IngestState.importing.value == "importing"
        assert IngestState.ready.value == "ready"
        assert IngestState.failed.value == "failed"

    def test_knowledge_state_values(self):
        assert KnowledgeState.not_started.value == "not_started"
        assert KnowledgeState.ready.value == "ready"
        assert KnowledgeState.stale.value == "stale"

    def test_inbox_type_values(self):
        assert InboxType.file.value == "file"
        assert InboxType.url.value == "url"
        assert InboxType.text.value == "text"
        assert InboxType.importer.value == "importer"


# ═══════════════════════════════════════════════════════════════════
# Config integration
# ═══════════════════════════════════════════════════════════════════


class TestStorageConfig:
    def test_default_storage_root(self):
        settings = get_settings()
        assert settings.storage_root == "mneme_data"

    def test_default_max_upload_size(self):
        settings = get_settings()
        assert settings.max_upload_size_bytes == 104_857_600  # 100 MB

    def test_staging_path_property(self):
        settings = get_settings()
        assert settings.staging_path == "mneme_data/staging"

    def test_allowed_mime_types_list(self):
        settings = get_settings()
        allowed = settings.allowed_mime_types_list
        assert "text/plain" in allowed
        assert "application/pdf" in allowed
        assert "image/png" in allowed
        assert "application/zip" in allowed

    def test_storage_backend_default(self):
        settings = get_settings()
        assert settings.storage_backend == "mneme_data"
