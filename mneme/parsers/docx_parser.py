"""Word (.docx) text extractor — A1 MVP.

Extracts structured text blocks from .docx files using python-docx.
Preserves heading hierarchy, paragraph content, and table structure.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from docx import Document


@dataclass
class BlockDraft:
    """A structured text block extracted from a document."""
    block_order: int
    content_markdown: str
    block_type: str  # "heading", "paragraph", "table"


def extract_docx(file_path: Path) -> list[BlockDraft]:
    """Extract structured text blocks from a .docx file path."""
    return _extract(Document(str(file_path)))


def extract_docx_bytes(content: bytes) -> list[BlockDraft]:
    """Extract structured text blocks from .docx file bytes.

    Use this when the file is read from a remote storage backend
    (S3, etc.) where a local file path is not available.
    """
    return _extract(Document(BytesIO(content)))


def _extract(doc: Document) -> list[BlockDraft]:
    """Core extraction logic shared by path and bytes entry points."""
    blocks: list[BlockDraft] = []
    order = 0

    for element in doc.element.body:
        tag = element.tag.split("}")[-1] if "}" in element.tag else element.tag

        if tag == "p":
            # Paragraph element — check if it's a heading
            style_name = _get_paragraph_style(element)
            text = _get_paragraph_text(element)
            if not text.strip():
                continue

            if style_name and style_name.startswith("Heading"):
                try:
                    level = int(style_name.replace("Heading", "").strip())
                except ValueError:
                    level = 1
                blocks.append(BlockDraft(
                    block_order=order,
                    content_markdown=f"{'#' * level} {text}",
                    block_type="heading",
                ))
            else:
                blocks.append(BlockDraft(
                    block_order=order,
                    content_markdown=text,
                    block_type="paragraph",
                ))
            order += 1

        elif tag == "tbl":
            # Table element
            md = _table_element_to_markdown(element)
            if md.strip():
                blocks.append(BlockDraft(
                    block_order=order,
                    content_markdown=md,
                    block_type="table",
                ))
                order += 1

    return blocks


def _get_paragraph_style(element) -> str | None:
    """Extract style name from a paragraph XML element."""
    from lxml import etree
    nsmap = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    pPr = element.find("w:pPr", nsmap)
    if pPr is not None:
        pStyle = pPr.find("w:pStyle", nsmap)
        if pStyle is not None:
            return pStyle.get(f"{{{nsmap['w']}}}val")
    return None


def _get_paragraph_text(element) -> str:
    """Extract text content from a paragraph XML element."""
    from lxml import etree
    nsmap = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    texts = []
    for run in element.findall("w:r", nsmap):
        for t in run.findall("w:t", nsmap):
            if t.text:
                texts.append(t.text)
    return "".join(texts)


def _table_element_to_markdown(element) -> str:
    """Convert a table XML element to Markdown table format."""
    from lxml import etree
    nsmap = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}

    rows_data: list[list[str]] = []
    for tr in element.findall("w:tr", nsmap):
        row: list[str] = []
        for tc in tr.findall("w:tc", nsmap):
            cell_text = ""
            for p in tc.findall("w:p", nsmap):
                t = _get_paragraph_text(p)
                if t.strip():
                    cell_text += t
            row.append(cell_text.strip())
        if any(cell for cell in row):
            rows_data.append(row)

    if not rows_data:
        return ""

    # Build Markdown table
    num_cols = max(len(r) for r in rows_data)
    # Pad rows to uniform column count
    for r in rows_data:
        while len(r) < num_cols:
            r.append("")

    lines = []
    # Header row
    lines.append("| " + " | ".join(rows_data[0]) + " |")
    # Separator
    lines.append("| " + " | ".join(["---"] * num_cols) + " |")
    # Data rows
    for row in rows_data[1:]:
        lines.append("| " + " | ".join(row) + " |")

    return "\n".join(lines)
