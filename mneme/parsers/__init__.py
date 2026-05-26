"""Mneme file parsers — MIME-type-based text extraction from uploaded files.

Usage::

    from mneme.parsers import get_parser

    parser = get_parser(mime_type)
    if parser:
        blocks = parser(file_path)
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from mneme.parsers.docx_parser import BlockDraft, extract_docx, extract_docx_bytes
from mneme.parsers.mime_detect import detect_mime_type
from mneme.parsers.ocr_parser import extract_image_bytes
from mneme.parsers.pdf_parser import extract_pdf_bytes
from mneme.parsers.txt_parser import extract_txt_bytes

# MIME type → parser function (accepts bytes, returns BlockDraft list)
PARSER_REGISTRY: dict[str, Callable[[bytes], list[BlockDraft]]] = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": extract_docx_bytes,
    "application/pdf": extract_pdf_bytes,
    "text/plain": extract_txt_bytes,
    "text/markdown": extract_txt_bytes,
    "image/png": extract_image_bytes,
    "image/jpeg": extract_image_bytes,
    "image/webp": extract_image_bytes,
    "image/bmp": extract_image_bytes,
}


def get_parser(mime_type: str) -> Callable[[bytes], list[BlockDraft]] | None:
    """Return the parser function for the given MIME type, or None."""
    return PARSER_REGISTRY.get(mime_type)


__all__ = ["BlockDraft", "detect_mime_type", "extract_docx", "extract_image_bytes", "get_parser", "PARSER_REGISTRY"]
