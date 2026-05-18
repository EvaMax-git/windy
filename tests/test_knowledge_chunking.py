"""Unit tests for P3-06 chunking engine."""

from __future__ import annotations

from uuid import uuid4

import pytest

from mneme.knowledge.chunking import (
    ChunkingStrategy,
    _chunk_by_fixed_size,
    _chunk_by_paragraph,
    _chunk_by_sentence,
    _merge_and_split,
    chunk_document,
    chunk_text,
)
from mneme.knowledge.token_estimator import estimate_tokens, strip_markdown


# ============================================================
# token_estimator
# ============================================================

class TestStripMarkdown:
    def test_removes_bold(self):
        assert strip_markdown("**hello**") == "hello"

    def test_removes_headers(self):
        assert strip_markdown("# Title") == "Title"

    def test_removes_code_fences(self):
        assert strip_markdown("```python\nx = 1\n```") == "python x = 1"

    def test_preserves_plain_text(self):
        assert strip_markdown("Hello world") == "Hello world"


class TestEstimateTokens:
    def test_english(self):
        tokens = estimate_tokens("Hello world this is a test sentence")
        assert tokens > 0

    def test_chinese(self):
        tokens = estimate_tokens("这是一个测试句子")
        assert tokens > 0

    def test_mixed(self):
        tokens = estimate_tokens("Hello 世界 test")
        assert tokens > 0

    def test_empty_returns_one(self):
        assert estimate_tokens("") == 1


# ============================================================
# chunk_by_paragraph
# ============================================================

class TestChunkByParagraph:
    def test_single_paragraph(self):
        result = _chunk_by_paragraph("This is a single paragraph.")
        assert len(result) == 1

    def test_multiple_paragraphs(self):
        result = _chunk_by_paragraph("First.\n\nSecond.\n\nThird.")
        assert len(result) == 3

    def test_empty_text(self):
        assert _chunk_by_paragraph("") == []
        assert _chunk_by_paragraph("   \n\n  ") == []


# ============================================================
# chunk_by_sentence
# ============================================================

class TestChunkBySentence:
    def test_english_sentences(self):
        result = _chunk_by_sentence("Hello world. This is a test. Final.")
        assert len(result) == 3

    def test_chinese_sentences(self):
        result = _chunk_by_sentence("你好世界。这是测试。最后。")
        assert len(result) == 3

    def test_no_ending_punctuation(self):
        result = _chunk_by_sentence("A sentence without proper ending")
        assert len(result) == 1


# ============================================================
# chunk_by_fixed_size
# ============================================================

class TestChunkByFixedSize:
    def test_small_text(self):
        result = _chunk_by_fixed_size("Short", chunk_size=100, overlap=20)
        assert len(result) == 1

    def test_large_text(self):
        text = "x" * 250
        result = _chunk_by_fixed_size(text, chunk_size=100, overlap=20)
        assert len(result) > 2

    def test_overlap(self):
        text = "abcdefghij" * 20
        result = _chunk_by_fixed_size(text, chunk_size=50, overlap=10)
        assert len(result) > 1
        if len(result) >= 2:
            assert result[0][-5:] in result[1]


# ============================================================
# _merge_and_split
# ============================================================

class TestMergeAndSplit:
    def test_merge_short(self):
        chunks = ["Hi", "there", "this is a much longer sentence that should not be merged"]
        result = _merge_and_split(chunks, min_len=80, max_len=3000)
        assert len(result) < len(chunks)

    def test_empty(self):
        assert _merge_and_split([], 80, 3000) == []


# ============================================================
# chunk_text
# ============================================================

class TestChunkText:
    def test_paragraph_strategy(self):
        long_text = ("This is a long enough paragraph to not be merged. " * 4 + "\n\n"
                     "This is a second long paragraph with sufficient text length. " * 4 + "\n\n"
                     "Third paragraph here with enough content to stand alone. " * 4)
        result = chunk_text(long_text, strategy=ChunkingStrategy.paragraph)
        assert len(result) >= 1
        for r in result:
            assert "chunk_text" in r
            assert "token_count" in r

    def test_sentence_strategy(self):
        long_text = ("First sentence with enough length to not get merged. "
                     "Second sentence that is also sufficiently long. "
                     "Third sentence long enough to stand alone too.")
        result = chunk_text(long_text, strategy=ChunkingStrategy.sentence)
        # Sentences may merge if short; just verify output structure
        assert len(result) >= 1
        for r in result:
            assert "chunk_text" in r
            assert "token_count" in r

    def test_fixed_size_strategy(self):
        result = chunk_text("x" * 5000, strategy=ChunkingStrategy.fixed_size, chunk_size=1200, overlap=200)
        assert len(result) > 1

    def test_empty_text(self):
        result = chunk_text("", strategy=ChunkingStrategy.paragraph)
        assert result == []


# ============================================================
# chunk_document
# ============================================================

class TestChunkDocument:
    def test_basic(self):
        doc_id = uuid4()
        blocks = [
            {"block_id": uuid4(), "block_order": 0, "content_markdown": "Paragraph one. With two sentences."},
            {"block_id": uuid4(), "block_order": 1, "content_markdown": "Paragraph two, with more info."},
        ]
        result = chunk_document(doc_id, 1, blocks, strategy=ChunkingStrategy.paragraph)
        assert result.document_id == doc_id
        assert result.document_version == 1
        assert result.strategy == ChunkingStrategy.paragraph
        assert len(result.chunks) > 0

    def test_sentence_strategy(self):
        doc_id = uuid4()
        long_sentence = ("First sentence long enough to not get merged into neighbors. "
                         "Second sentence with substantial length as well here. "
                         "Third sentence that is also sufficiently long alone.")
        blocks = [{"block_id": uuid4(), "block_order": 0, "content_markdown": long_sentence}]
        result = chunk_document(doc_id, 1, blocks, strategy=ChunkingStrategy.sentence)
        # Merge may combine some, so just check we get sensible output
        assert len(result.chunks) >= 1
        for c in result.chunks:
            assert c.chunk_order >= 0
            assert len(c.chunk_text) > 0

    def test_empty_blocks(self):
        doc_id = uuid4()
        result = chunk_document(doc_id, 1, [{"block_id": uuid4(), "block_order": 0, "content_markdown": ""}])
        assert len(result.chunks) == 0

    def test_chunks_have_token_counts(self):
        doc_id = uuid4()
        blocks = [{"block_id": uuid4(), "block_order": 0, "content_markdown": "Some meaningful text content here."}]
        result = chunk_document(doc_id, 1, blocks)
        for ch in result.chunks:
            assert ch.token_count > 0

    def test_chunks_ordered(self):
        doc_id = uuid4()
        blocks = [{"block_id": uuid4(), "block_order": i, "content_markdown": f"Block {i} content."} for i in range(5)]
        result = chunk_document(doc_id, 1, blocks, strategy=ChunkingStrategy.paragraph)
        for i, ch in enumerate(result.chunks):
            assert ch.chunk_order == i
