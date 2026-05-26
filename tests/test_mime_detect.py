"""Tests for the MIME type detection module (W1 A-07)."""

import pytest

from mneme.parsers.mime_detect import detect_mime_type


# ── Suffix mapping ────────────────────────────────────────────────────────


def test_detect_pdf_suffix():
    assert detect_mime_type("report.pdf", b"") == "application/pdf"


def test_detect_docx_suffix():
    mime = detect_mime_type("doc.docx", b"")
    assert mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def test_detect_txt_suffix():
    assert detect_mime_type("notes.txt", b"hello") == "text/plain"


def test_detect_md_suffix():
    assert detect_mime_type("readme.md", b"# Title") == "text/markdown"


def test_detect_png_suffix():
    assert detect_mime_type("image.png", b"\x89PNG") == "image/png"


def test_detect_jpg_suffix():
    assert detect_mime_type("photo.jpg", b"\xff\xd8\xff") == "image/jpeg"


def test_detect_jpeg_suffix():
    assert detect_mime_type("photo.jpeg", b"\xff\xd8\xff") == "image/jpeg"


def test_detect_webp_suffix():
    assert detect_mime_type("anim.webp", b"RIFF") == "image/webp"


def test_detect_bmp_suffix():
    assert detect_mime_type("bitmap.bmp", b"BM") == "image/bmp"


# ── Case insensitivity ────────────────────────────────────────────────────


def test_detect_uppercase_suffix():
    assert detect_mime_type("REPORT.PDF", b"") == "application/pdf"


def test_detect_mixed_case_suffix():
    assert detect_mime_type("file.TxT", b"hi") == "text/plain"


# ── Magic bytes fallback ─────────────────────────────────────────────────


def test_detect_pdf_magic_bytes():
    # PDF magic bytes: %PDF
    mime = detect_mime_type("unknown_file", b"%PDF-1.4 some content")
    assert mime == "application/pdf"


def test_detect_png_magic_bytes():
    """python-magic detects PNG from magic bytes when suffix is absent."""
    import io
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(buf, format="PNG")
    mime = detect_mime_type("unknown_file", buf.getvalue())
    assert mime == "image/png"


# ── Unknown type ──────────────────────────────────────────────────────────


def test_detect_unknown_fallback():
    mime = detect_mime_type("random.xyz", b"\x00\x01\x02")
    assert mime == "application/octet-stream"


# ── Integration with get_parser ───────────────────────────────────────────


def test_detect_then_get_parser():
    """detect_mime_type result should work with get_parser."""
    from mneme.parsers import get_parser

    mime = detect_mime_type("doc.pdf", b"")
    parser = get_parser(mime)
    assert parser is not None


def test_detect_txt_then_parse():
    """End-to-end: detect MIME -> parse content."""
    from mneme.parsers import get_parser

    content = "Hello World\n\nSecond paragraph.".encode("utf-8")
    mime = detect_mime_type("file.txt", content)
    parser = get_parser(mime)
    assert parser is not None
    blocks = parser(content)
    assert len(blocks) == 2
