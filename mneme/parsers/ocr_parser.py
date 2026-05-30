"""Image OCR extractor — W1 A-06.

Extracts text from images (PNG, JPG, etc.) using PaddleOCR.
Supports Chinese and English text recognition.
Groups detected text lines into paragraphs by vertical proximity.
"""

from __future__ import annotations

from mneme.parsers.docx_parser import BlockDraft


_ocr_instance = None


def _get_ocr():
    """Return a cached PaddleOCR instance (created once per process)."""
    global _ocr_instance
    if _ocr_instance is None:
        import os
        os.environ.setdefault("FLAGS_use_mkldnn", "0")
        from paddleocr import PaddleOCR
        _ocr_instance = PaddleOCR(lang="ch", enable_mkldnn=False)
    return _ocr_instance


def extract_image_bytes(content: bytes) -> list[BlockDraft]:
    """Extract structured text blocks from image bytes via OCR.

    Uses PaddleOCR (lang='ch') for Chinese + English recognition.
    Lines are grouped into paragraphs by vertical proximity:
    lines closer than 1.5× the median line height are merged.
    """
    import io

    import numpy as np
    from PIL import Image

    img = Image.open(io.BytesIO(content)).convert("RGB")
    img_array = np.array(img)

    ocr = _get_ocr()
    result = ocr.predict(img_array)

    if not result:
        return []

    page = result[0]
    texts = page.get("rec_texts", [])
    polys = page.get("rec_polys", [])

    if not texts:
        return []

    # Convert polys to list[list[list[float]]] format for _group_into_paragraphs
    lines: list[tuple[list[list[float]], str]] = []
    for poly, text in zip(polys, texts):
        bbox = [[float(pt[0]), float(pt[1])] for pt in poly.tolist()]
        lines.append((bbox, text))

    if not lines:
        return []

    paragraphs = _group_into_paragraphs(lines)

    return [
        BlockDraft(
            block_order=i,
            content_markdown=text,
            block_type="paragraph",
        )
        for i, text in enumerate(paragraphs)
    ]


def _group_into_paragraphs(
    lines: list[tuple[list[list[float]], str]],
) -> list[str]:
    """Group OCR text lines into paragraphs by vertical proximity.

    Uses the midpoint Y of each bounding box. Lines whose midpoints
    are within 1.5× the median line height are merged into one paragraph.
    """
    if len(lines) <= 1:
        return [text for _, text in lines]

    # Calculate mid-Y and line height for each detection
    entries: list[tuple[float, float, str]] = []
    for bbox, text in lines:
        ys = [pt[1] for pt in bbox]
        mid_y = sum(ys) / len(ys)
        height = max(ys) - min(ys)
        entries.append((mid_y, height, text))

    # Sort top-to-bottom
    entries.sort(key=lambda e: e[0])

    # Median line height for gap threshold
    heights = [h for _, h, _ in entries if h > 0]
    median_h = sorted(heights)[len(heights) // 2] if heights else 20.0
    gap_threshold = median_h * 1.5

    # Group consecutive lines with small vertical gaps
    paragraphs: list[str] = []
    current_lines: list[str] = [entries[0][2]]
    prev_y = entries[0][0]

    for mid_y, _h, text in entries[1:]:
        if mid_y - prev_y > gap_threshold:
            paragraphs.append(" ".join(current_lines))
            current_lines = [text]
        else:
            current_lines.append(text)
        prev_y = mid_y

    paragraphs.append(" ".join(current_lines))
    return paragraphs
