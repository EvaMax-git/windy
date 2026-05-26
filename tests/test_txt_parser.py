"""Tests for the TXT parser module (W1 A-05: 纯文字读取)."""

from pathlib import Path

import pytest

from mneme.parsers.docx_parser import BlockDraft
from mneme.parsers.txt_parser import extract_txt_bytes


# ── UTF-8 编码 ────────────────────────────────────────────────────────────


def test_extract_utf8_basic():
    """UTF-8 文本正确提取，按空行分段。"""
    content = "第一段内容。\n\n第二段内容。".encode("utf-8")
    blocks = extract_txt_bytes(content)
    assert len(blocks) == 2
    assert "第一段" in blocks[0].content_markdown
    assert "第二段" in blocks[1].content_markdown


def test_extract_utf8_block_order():
    """block_order 从 0 递增。"""
    content = "段一\n\n段二\n\n段三".encode("utf-8")
    blocks = extract_txt_bytes(content)
    assert [b.block_order for b in blocks] == [0, 1, 2]


def test_extract_utf8_block_type():
    """所有块类型为 paragraph。"""
    content = "内容。".encode("utf-8")
    blocks = extract_txt_bytes(content)
    assert blocks[0].block_type == "paragraph"


def test_extract_utf8_chinese():
    """中文内容不乱码。"""
    content = "知识管理系统，统一管理资产与记忆。".encode("utf-8")
    blocks = extract_txt_bytes(content)
    assert "知识管理系统" in blocks[0].content_markdown


# ── GBK 编码 ─────────────────────────────────────────────────────────────


def test_extract_gbk_auto_detect():
    """GBK 编码自动检测。"""
    content = "中文内容GBK编码。".encode("gbk")
    blocks = extract_txt_bytes(content)
    assert len(blocks) >= 1
    assert "中文内容" in blocks[0].content_markdown


def test_extract_gbk_fixture():
    """从 GBK 测试文件提取。"""
    fixture = Path("tests/fixtures/test_gbk.txt")
    if not fixture.exists():
        pytest.skip("GBK 测试文件不存在")
    blocks = extract_txt_bytes(fixture.read_bytes())
    assert len(blocks) >= 1


# ── 边界情况 ─────────────────────────────────────────────────────────────


def test_extract_empty_content():
    """空内容返回空列表。"""
    blocks = extract_txt_bytes(b"")
    assert blocks == []


def test_extract_whitespace_only():
    """纯空白返回空列表。"""
    blocks = extract_txt_bytes(b"   \n\n   \n  ")
    assert blocks == []


def test_extract_single_paragraph():
    """单段落返回一个块。"""
    content = "唯一的一段内容。".encode("utf-8")
    blocks = extract_txt_bytes(content)
    assert len(blocks) == 1


def test_extract_no_double_newline():
    """无空行分隔时，整个文本为一个块。"""
    content = "第一行\n第二行\n第三行".encode("utf-8")
    blocks = extract_txt_bytes(content)
    assert len(blocks) == 1
    assert "第一行" in blocks[0].content_markdown


# ── 解析器注册 ───────────────────────────────────────────────────────────


def test_get_parser_registered():
    """txt_parser 应注册到 PARSER_REGISTRY。"""
    from mneme.parsers import get_parser

    parser = get_parser("text/plain")
    assert parser is not None


def test_get_parser_markdown():
    """text/markdown 也使用 txt_parser。"""
    from mneme.parsers import get_parser

    parser = get_parser("text/markdown")
    assert parser is not None
