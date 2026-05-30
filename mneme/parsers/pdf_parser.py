"""PDF text extractor — W1 A-03.

Extracts text blocks from PDF files using PyMuPDF (fitz).
Supports Chinese text without encoding issues.
Falls back to OCR for scanned (image-only) PDFs.
"""

from __future__ import annotations

import logging

from mneme.parsers.docx_parser import BlockDraft

logger = logging.getLogger(__name__)

# Minimum text length to consider a page as having extractable text
_MIN_TEXT_LEN = 20


def extract_pdf_bytes(content: bytes) -> list[BlockDraft]:
    """Extract structured text blocks from PDF bytes.

    Uses PyMuPDF (fitz) for text extraction.
    For scanned PDFs where get_text() returns almost nothing,
    falls back to rendering pages as images and running OCR.
    Each page's text is split into paragraphs on blank lines.
    """
    import fitz

    blocks: list[BlockDraft] = []
    order = 0
    ocr_pages: list[tuple[int, bytes]] = []  # (page_index, png_bytes)

    with fitz.open(stream=content, filetype="pdf") as doc:
        for page_idx, page in enumerate(doc):
            text = page.get_text()

            # If the page has almost no text, treat it as scanned
            if len(text.strip()) < _MIN_TEXT_LEN:
                logger.debug("Page %d appears scanned (text len=%d), queuing for OCR", page_idx, len(text.strip()))
                pix = page.get_pixmap()
                ocr_pages.append((page_idx, pix.tobytes("png")))
                continue

            paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
            for para in paragraphs:
                blocks.append(BlockDraft(
                    block_order=order,
                    content_markdown=para,
                    block_type="paragraph",
                ))
                order += 1

    # Run OCR on scanned pages
    for page_idx, img_bytes in ocr_pages:
        logger.info("Running OCR on scanned page %d", page_idx)
        try:
            from mneme.parsers.ocr_parser import extract_image_bytes
            ocr_blocks = extract_image_bytes(img_bytes)
            for b in ocr_blocks:
                blocks.append(BlockDraft(
                    block_order=order,
                    content_markdown=b.content_markdown,
                    block_type="paragraph",
                ))
                order += 1
        except Exception:
            logger.warning("OCR failed for page %d, skipping", page_idx, exc_info=True)

    return blocks
