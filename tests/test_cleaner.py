"""Tests for mneme.processing.cleaner.clean_text()."""

import pytest
from mneme.processing.cleaner import clean_text


class TestCleanWhitespace:
    """A-09: Consolidate multiple spaces/tabs into single space."""

    def test_consolidate_spaces(self):
        assert clean_text("hello     world") == "hello world"

    def test_consolidate_tabs(self):
        assert clean_text("hello\t\t\tworld") == "hello world"

    def test_preserve_single_spaces(self):
        assert clean_text("hello world") == "hello world"


class TestCleanEmptyLines:
    """A-10: Merge multiple empty lines into single newline."""

    def test_merge_multiple_empty_lines(self):
        text = "line1\n\n\n\nline2"
        assert clean_text(text) == "line1\n\nline2"

    def test_preserve_single_newline(self):
        text = "line1\nline2"
        assert clean_text(text) == "line1\nline2"


class TestCleanGarbled:
    """A-11: Filter garbled/control characters."""

    def test_remove_control_characters(self):
        # \x00 and \x01 are control chars that should be removed
        text = "hello\x00\x01world"
        assert clean_text(text) == "helloworld"

    def test_preserve_chinese(self):
        text = "你好世界"
        assert clean_text(text) == "你好世界"

    def test_preserve_newlines_tabs(self):
        text = "a\nb\tc"
        assert clean_text(text) == "a\nb\tc"

    def test_preserve_cjk_punctuation(self):
        """CJK punctuation like ，。、；： must survive cleaning."""
        text = "你好，世界。测试；内容：结束"
        assert clean_text(text) == text


class TestCRLFNormalization:
    """Line ending normalization: CRLF → LF, standalone CR → LF."""

    def test_crlf_blank_lines_merged(self):
        """CRLF blank lines must be merged just like LF."""
        text = "line1\r\n\r\n\r\n\r\nline2"
        result = clean_text(text)
        assert "\n\n\n" not in result
        assert "line1" in result
        assert "line2" in result

    def test_crlf_normal_lines_preserved(self):
        text = "line1\r\nline2\r\nline3"
        result = clean_text(text)
        assert "\r" not in result
        assert "line1" in result

    def test_standalone_cr_converted(self):
        text = "line1\r\r\rline2"
        result = clean_text(text)
        assert "\r" not in result
        assert "\n\n\n" not in result


class TestCleanHeadersFooters:
    """A-12: Remove page header/footer patterns."""

    def test_remove_page_number_format1(self):
        """第 X 页 pattern."""
        text = "正文内容\n第 1 页\n更多内容"
        result = clean_text(text)
        assert "第 1 页" not in result
        assert "正文内容" in result

    def test_remove_page_number_format2(self):
        """- X - pattern."""
        text = "正文内容\n- 3 -\n更多内容"
        result = clean_text(text)
        assert "- 3 -" not in result
        assert "正文内容" in result

    def test_remove_page_number_format3(self):
        """Page X pattern."""
        text = "正文内容\nPage 5\n更多内容"
        result = clean_text(text)
        assert "Page 5" not in result
        assert "正文内容" in result

    def test_footer_removal_merges_blank_lines(self):
        """Removing a footer line must not leave 3+ consecutive newlines."""
        text = "内容A\nPage 1\n\n内容B"
        result = clean_text(text)
        assert "\n\n\n" not in result
        assert "内容A" in result
        assert "内容B" in result
