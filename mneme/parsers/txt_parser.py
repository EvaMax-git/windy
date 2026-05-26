"""Plain text (.txt, .md, .csv) extractor — W1 A-05.

Extracts structured text blocks from plain text files.
Auto-detects encoding (UTF-8, GBK, etc.) using chardet.
Splits on blank lines to form paragraph blocks.
"""

from __future__ import annotations

from mneme.parsers.docx_parser import BlockDraft


def extract_txt_bytes(content: bytes) -> list[BlockDraft]:
    """Extract structured text blocks from plain text bytes.

    Auto-detects encoding via chardet, splits on blank lines.
    Each non-empty paragraph becomes a BlockDraft.
    """
    import chardet

    detected = chardet.detect(content)
    encoding = detected.get("encoding") or "utf-8"
    text = content.decode(encoding, errors="replace")

    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]

    return [
        BlockDraft(
            block_order=i,
            content_markdown=para,
            block_type="paragraph",
        )
        for i, para in enumerate(paragraphs)
    ]
