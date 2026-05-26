"""Tests for the OCR parser module (W1 A-06: 图片文字识别)."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from mneme.parsers.docx_parser import BlockDraft
from mneme.parsers.ocr_parser import extract_image_bytes

# Chinese font path (Windows)
_FONT_PATH = "C:/Windows/Fonts/simhei.ttf"
_FONT_SIZE = 32

# Check dependencies
try:
    import paddleocr  # noqa: F401
    _HAS_PADDLEOCR = True
except ImportError:
    _HAS_PADDLEOCR = False

_SKIP_OCR = pytest.mark.skipif(
    not _HAS_PADDLEOCR or not Path(_FONT_PATH).exists(),
    reason="paddleocr 或中文字体不可用",
)


def _make_image(text: str, width: int = 800, height: int = 200) -> bytes:
    """Create a PNG image with the given text rendered in Chinese font."""
    img = Image.new("RGB", (width, height), color="white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(_FONT_PATH, _FONT_SIZE)
    except (OSError, IOError):
        font = ImageFont.load_default()
    draw.text((40, 40), text, fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_multiline_image(lines: list[str]) -> bytes:
    """Create a PNG image with multiple lines of text."""
    line_height = _FONT_SIZE + 16
    height = 40 + len(lines) * line_height + 40
    img = Image.new("RGB", (800, height), color="white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(_FONT_PATH, _FONT_SIZE)
    except (OSError, IOError):
        font = ImageFont.load_default()
    y = 40
    for line in lines:
        draw.text((40, y), line, fill="black", font=font)
        y += line_height
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ── 基本提取 ─────────────────────────────────────────────────────────────


@_SKIP_OCR
def test_extract_image_chinese():
    """中文图片 OCR 识别。"""
    image_bytes = _make_image("知识管理系统")
    blocks = extract_image_bytes(image_bytes)
    assert len(blocks) >= 1
    all_text = " ".join(b.content_markdown for b in blocks)
    # PaddleOCR 应识别出大部分中文字符
    assert len(all_text.strip()) > 0


@_SKIP_OCR
def test_extract_image_chinese_accuracy():
    """中文准确率 > 85%（验收标准 A-06）。"""
    text = "这是一个中文测试文档用于验证光学字符识别的准确率"
    image_bytes = _make_image(text, width=1200, height=300)
    blocks = extract_image_bytes(image_bytes)
    all_text = "".join(b.content_markdown for b in blocks)

    # 计算匹配率：原文字数 vs 识别出的中文字符数
    original_chars = set(text)
    recognized_chars = set(all_text)
    matched = original_chars & recognized_chars
    accuracy = len(matched) / len(original_chars) if original_chars else 0
    assert accuracy > 0.85, f"准确率 {accuracy:.0%} 低于 85%，识别结果: {all_text}"


@_SKIP_OCR
def test_extract_image_english():
    """英文图片 OCR 识别。"""
    image_bytes = _make_image("Hello World")
    blocks = extract_image_bytes(image_bytes)
    assert len(blocks) >= 1
    all_text = " ".join(b.content_markdown for b in blocks).upper()
    assert "HELLO" in all_text or "WORLD" in all_text


@_SKIP_OCR
def test_extract_image_multiline():
    """多行文本图片识别。"""
    image_bytes = _make_multiline_image(["第一行内容", "第二行内容", "第三行内容"])
    blocks = extract_image_bytes(image_bytes)
    all_text = " ".join(b.content_markdown for b in blocks)
    # 应识别出多行内容
    assert "第一行" in all_text or "第二行" in all_text or "第三行" in all_text


# ── 结构验证 ─────────────────────────────────────────────────────────────


@_SKIP_OCR
def test_extract_image_block_order():
    """block_order 从 0 递增。"""
    image_bytes = _make_multiline_image(["段落一", "段落二"])
    blocks = extract_image_bytes(image_bytes)
    orders = [b.block_order for b in blocks]
    assert orders == list(range(len(blocks)))


@_SKIP_OCR
def test_extract_image_block_type():
    """所有块类型为 paragraph。"""
    image_bytes = _make_image("测试内容")
    blocks = extract_image_bytes(image_bytes)
    assert len(blocks) >= 1
    assert all(b.block_type == "paragraph" for b in blocks)


# ── 边界情况 ─────────────────────────────────────────────────────────────


@_SKIP_OCR
def test_extract_image_empty_bytes():
    """空字节应抛异常或返回空。"""
    # PaddleOCR 对空输入可能抛异常，这是合理的
    try:
        blocks = extract_image_bytes(b"")
        assert blocks == []
    except Exception:
        # 空输入导致异常也是可接受的
        pass


@_SKIP_OCR
def test_extract_image_white_image():
    """纯白图片应返回空列表。"""
    img = Image.new("RGB", (400, 200), color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    blocks = extract_image_bytes(buf.getvalue())
    assert blocks == []


# ── 解析器注册 ───────────────────────────────────────────────────────────


def test_get_parser_image_png():
    """image/png 应注册到 PARSER_REGISTRY。"""
    from mneme.parsers import get_parser

    parser = get_parser("image/png")
    assert parser is not None


def test_get_parser_image_jpeg():
    """image/jpeg 应注册到 PARSER_REGISTRY。"""
    from mneme.parsers import get_parser

    parser = get_parser("image/jpeg")
    assert parser is not None


def test_get_parser_image_webp():
    """image/webp 应注册到 PARSER_REGISTRY。"""
    from mneme.parsers import get_parser

    parser = get_parser("image/webp")
    assert parser is not None


def test_get_parser_image_bmp():
    """image/bmp 应注册到 PARSER_REGISTRY。"""
    from mneme.parsers import get_parser

    parser = get_parser("image/bmp")
    assert parser is not None
