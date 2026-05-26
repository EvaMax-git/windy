"""PDF text extractor — W1 A-03.

Extracts text blocks from PDF files using PyMuPDF (fitz).
Supports Chinese text without encoding issues.
Splits each page's text into paragraphs on double newlines.
"""

from __future__ import annotations

from mneme.parsers.docx_parser import BlockDraft


def extract_pdf_bytes(content: bytes) -> list[BlockDraft]:
    """Extract structured text blocks from PDF bytes.

    Uses PyMuPDF (fitz) for text extraction.
    Chinese text is natively supported without encoding issues.
    Each page's text is split into paragraphs on blank lines.
    """
    import fitz

    doc = fitz.open(stream=content, filetype="pdf")
    blocks: list[BlockDraft] = []
    order = 0

    for page in doc:
        text = page.get_text()
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        for para in paragraphs:
            blocks.append(BlockDraft(
                block_order=order,
                content_markdown=para,
                block_type="paragraph",
            ))
            order += 1

    doc.close()
    return blocks
