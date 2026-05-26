"""MIME type detection — W1 A-07.

Detects file MIME type via suffix mapping + python-magic fallback.
"""

from __future__ import annotations

import filetype

# Suffix → MIME mapping
_SUFFIX_MAP: dict[str, str] = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


def detect_mime_type(filename: str, content: bytes) -> str:
    """Detect MIME type by suffix first, then magic bytes fallback.

    Priority: suffix mapping -> python-magic -> application/octet-stream
    """
    import os

    ext = os.path.splitext(filename)[1].lower()
    if ext in _SUFFIX_MAP:
        return _SUFFIX_MAP[ext]

    try:
        kind = filetype.guess(content)
        if kind is not None:
            return kind.mime
    except Exception:
        pass
    return "application/octet-stream"
