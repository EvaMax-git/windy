"""Tests for the PDF parser module (W1 A-03: PDF 文字提取)."""

from pathlib import Path

import pytest

from mneme.parsers.docx_parser import BlockDraft
from mneme.parsers.pdf_parser import extract_pdf_bytes


# ── 辅助：生成测试 PDF ───────────────────────────────────────────────────


def _make_pdf(path: Path, pages: list[str]) -> Path:
    """Create a PDF with the given page texts using PyMuPDF.

    Uses insert_htmlbox for proper Chinese rendering.
    """
    import fitz

    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        rect = fitz.Rect(72, 72, page.rect.width - 72, page.rect.height - 72)
        page.insert_htmlbox(rect, text)
    doc.save(str(path))
    doc.close()
    return path


# ── 基本提取 ─────────────────────────────────────────────────────────────


def test_extract_pdf_basic(tmp_path):
    """基本 PDF 文字提取。"""
    pdf = _make_pdf(tmp_path / "basic.pdf", ["这是第一页的内容。"])
    blocks = extract_pdf_bytes(pdf.read_bytes())
    assert len(blocks) >= 1
    assert "第一页" in blocks[0].content_markdown


def test_extract_pdf_chinese(tmp_path):
    """中文内容不乱码。"""
    pdf = _make_pdf(tmp_path / "chinese.pdf", ["知识管理系统，统一管理资产与记忆。"])
    blocks = extract_pdf_bytes(pdf.read_bytes())
    all_text = " ".join(b.content_markdown for b in blocks)
    assert "知识管理系统" in all_text
    assert "统一管理" in all_text


def test_extract_pdf_multi_page(tmp_path):
    """全部页提取，不只是第一页。"""
    pdf = _make_pdf(tmp_path / "multi.pdf", [
        "第一页内容。",
        "第二页内容。",
        "第三页内容。",
    ])
    blocks = extract_pdf_bytes(pdf.read_bytes())
    all_text = " ".join(b.content_markdown for b in blocks)
    assert "第一页" in all_text
    assert "第二页" in all_text
    assert "第三页" in all_text


def test_extract_pdf_block_order(tmp_path):
    """block_order 从 0 递增。"""
    pdf = _make_pdf(tmp_path / "order.pdf", ["段一", "段二", "段三"])
    blocks = extract_pdf_bytes(pdf.read_bytes())
    orders = [b.block_order for b in blocks]
    assert orders == list(range(len(blocks)))


def test_extract_pdf_block_type(tmp_path):
    """所有块类型为 paragraph。"""
    pdf = _make_pdf(tmp_path / "type.pdf", ["内容。"])
    blocks = extract_pdf_bytes(pdf.read_bytes())
    assert all(b.block_type == "paragraph" for b in blocks)


# ── 边界情况 ─────────────────────────────────────────────────────────────


def test_extract_pdf_empty(tmp_path):
    """空 PDF 返回空列表。"""
    import fitz

    doc = fitz.open()
    doc.new_page()
    pdf_path = tmp_path / "empty.pdf"
    doc.save(str(pdf_path))
    doc.close()

    blocks = extract_pdf_bytes(pdf_path.read_bytes())
    assert blocks == []


def test_extract_pdf_real_file():
    """使用真实 PDF 文件测试。"""
    pdf_path = Path("mneme_data/staging/document_1777806251001_2193135.pdf")
    if not pdf_path.exists():
        pytest.skip("测试 PDF 文件不存在")
    # 检查文件是否为有效 PDF（至少需要 PDF header）
    content = pdf_path.read_bytes()
    if not content.startswith(b"%PDF"):
        pytest.skip("测试文件不是有效 PDF")
    blocks = extract_pdf_bytes(content)
    assert len(blocks) > 0
    # 验证中文不乱码
    all_text = " ".join(b.content_markdown for b in blocks)
    assert any(ord(c) > 0x4E00 for c in all_text)


def test_extract_pdf_generated_chinese(tmp_path):
    """用 PyMuPDF 生成含中文的 PDF 并提取。"""
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    rect = fitz.Rect(72, 72, page.rect.width - 72, page.rect.height - 72)
    page.insert_htmlbox(rect, "这是中文测试内容，用于验证 PDF 解析器。")
    pdf_path = tmp_path / "gen_chinese.pdf"
    doc.save(str(pdf_path))
    doc.close()

    blocks = extract_pdf_bytes(pdf_path.read_bytes())
    assert len(blocks) >= 1
    all_text = " ".join(b.content_markdown for b in blocks)
    assert "中文测试" in all_text


# ── 解析器注册 ───────────────────────────────────────────────────────────


def test_get_parser_registered():
    """pdf_parser 应注册到 PARSER_REGISTRY。"""
    from mneme.parsers import get_parser

    parser = get_parser("application/pdf")
    assert parser is not None
