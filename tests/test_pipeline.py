"""Tests for mneme.processing.pipeline.process_file()."""

import pytest
from mneme.processing.pipeline import process_file


class TestProcessFileBasic:
    """A-14: Pipeline integration — detect → parse → clean → chunk."""

    def test_txt_file_returns_expected_keys(self):
        content = "这是第一段内容，用于测试。\n\n这是第二段内容，也需要测试。\n\n这是第三段。".encode("utf-8")
        result = process_file("test.txt", content)
        assert "chunks" in result
        assert "char_count" in result
        assert "file_type" in result
        assert "filename" in result
        assert "size" in result

    def test_txt_file_type(self):
        content = "hello world".encode("utf-8")
        result = process_file("test.txt", content)
        assert result["file_type"] == "text/plain"

    def test_filename_preserved(self):
        content = "content".encode("utf-8")
        result = process_file("myfile.txt", content)
        assert result["filename"] == "myfile.txt"

    def test_size_matches_input(self):
        content = "测试内容".encode("utf-8")
        result = process_file("test.txt", content)
        assert result["size"] == len(content)

    def test_char_count_positive(self):
        content = "这是一段有足够内容的测试文本，用于验证字数统计功能。".encode("utf-8")
        result = process_file("test.txt", content)
        assert result["char_count"] > 0

    def test_chunks_are_strings(self):
        content = ("段落一。" * 50 + "\n\n" + "段落二。" * 50).encode("utf-8")
        result = process_file("test.txt", content)
        for chunk in result["chunks"]:
            assert isinstance(chunk, str)


class TestProcessFileChunking:
    """A-13/A-14: Chunking with 500 chars / 50 overlap."""

    def test_long_text_produces_multiple_chunks(self):
        # ~5000 chars should produce ~10 chunks at 500/50
        text = "这是一段测试文本，用于验证分块功能。" * 200
        content = text.encode("utf-8")
        result = process_file("test.txt", content)
        assert len(result["chunks"]) > 1

    def test_short_text_single_chunk(self):
        text = "短文本"
        content = text.encode("utf-8")
        result = process_file("test.txt", content)
        assert len(result["chunks"]) >= 1


class TestProcessFileErrors:
    """A-14: Error handling."""

    def test_unsupported_file_type_raises(self):
        with pytest.raises(ValueError, match="不支持"):
            process_file("test.xyz", b"content")

    def test_empty_txt_returns_empty_chunks(self):
        content = "".encode("utf-8")
        result = process_file("empty.txt", content)
        assert result["chunks"] == []
        assert result["char_count"] == 0


class TestProcessFileMarkdown:
    """Test with .md files (uses txt parser)."""

    def test_markdown_file(self):
        content = "# 标题\n\n正文内容。".encode("utf-8")
        result = process_file("test.md", content)
        assert result["file_type"] == "text/markdown"
        assert len(result["chunks"]) >= 1


class TestLargeFile:
    """A-16: Large file handling — pipeline should not crash or OOM."""

    def test_large_txt_processes_successfully(self):
        """~1MB text file should process without issues."""
        paragraph = "这是一段测试文本，用于验证大文件处理能力。包含中文和English混合内容。\n\n"
        # ~1MB of text
        big_text = paragraph * 20000
        content = big_text.encode("utf-8")
        result = process_file("big.txt", content)
        assert len(result["chunks"]) > 0
        assert result["size"] == len(content)
        assert result["char_count"] > 0
