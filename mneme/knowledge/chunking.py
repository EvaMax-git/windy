"""Knowledge chunking engine (P3-06).

Strategies
----------
* ``paragraph`` — split on double-newline boundaries, merge short paragraphs.
* ``sentence`` — split on sentence-ending punctuation + newline, enforce min/max length.
* ``fixed_size`` — fixed char window with overlap.

Core entry point: :func:`chunk_document` reads a document's blocks,
concatenates their text, applies the chosen strategy, and returns chunk records
(nothing persisted — that's the caller's job).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from uuid import UUID

from mneme.knowledge.token_estimator import estimate_tokens, strip_markdown


class ChunkingStrategy(str, Enum):
    paragraph = "paragraph"
    sentence = "sentence"
    fixed_size = "fixed_size"


# ── Defaults ──────────────────────────────────────────────────────────
DEFAULT_CHUNK_SIZE = 1200      # chars per chunk for fixed_size
DEFAULT_OVERLAP = 200          # overlap for fixed_size
MIN_CHUNK_LENGTH = 80          # merge chunks shorter than this
MAX_CHUNK_LENGTH = 3000        # force-split chunks longer than this

_SENTENCE_END = re.compile(r"(?<=[。！？.!?\n])\s*")


@dataclass
class ChunkRecord:
    """In-memory chunk before persistence."""
    chunk_order: int
    chunk_text: str
    token_count: int
    block_id: UUID | None = None
    span_start: int = 0
    span_end: int = 0


@dataclass
class ChunkResult:
    """Result of chunking a document."""
    document_id: UUID
    document_version: int
    chunks: list[ChunkRecord]
    strategy: ChunkingStrategy


# ═══════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════


def chunk_document(
    document_id: UUID,
    document_version: int,
    blocks: list[dict[str, Any]],
    *,
    strategy: ChunkingStrategy = ChunkingStrategy.paragraph,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_OVERLAP,
) -> ChunkResult:
    """Chunk a document's blocks into smaller text segments.

    Args:
        document_id: Parent document UUID.
        document_version: Version of the document at chunk time.
        blocks: List of dicts with keys: ``block_id``, ``content_markdown``, ``block_order``.
        strategy: Chunking strategy to apply.
        chunk_size: Target char count for fixed_size strategy.
        overlap: Overlap chars for fixed_size strategy.

    Returns:
        :class:`ChunkResult` with ordered chunks.
    """
    # Sort blocks by order and build concatenated plain text with block-id mapping
    sorted_blocks = sorted(blocks, key=lambda b: b.get("block_order", 0))

    # Build a plain text concat with block_id annotations for span tracking
    texts: list[str] = []
    block_spans: list[tuple[UUID, int, int]] = []  # (block_id, start, end) in cumulative text

    offset = 0
    for b in sorted_blocks:
        plain = strip_markdown(b["content_markdown"])
        if not plain.strip():
            continue
        texts.append(plain)
        end = offset + len(plain)
        block_spans.append((b["block_id"], offset, end))
        offset = end + 1  # +1 for the separator

    full_text = "\n".join(texts)
    if not full_text.strip():
        return ChunkResult(
            document_id=document_id,
            document_version=document_version,
            chunks=[],
            strategy=strategy,
        )

    # Choose chunker
    if strategy == ChunkingStrategy.paragraph:
        raw_chunks = _chunk_by_paragraph(full_text)
    elif strategy == ChunkingStrategy.sentence:
        raw_chunks = _chunk_by_sentence(full_text)
    elif strategy == ChunkingStrategy.fixed_size:
        raw_chunks = _chunk_by_fixed_size(full_text, chunk_size=chunk_size, overlap=overlap)
    else:
        raw_chunks = _chunk_by_paragraph(full_text)

    # Post-process: merge short, split long, assign block_id
    chunks = _post_process(raw_chunks, block_spans)

    return ChunkResult(
        document_id=document_id,
        document_version=document_version,
        chunks=chunks,
        strategy=strategy,
    )


def chunk_text(
    text: str,
    *,
    strategy: ChunkingStrategy = ChunkingStrategy.paragraph,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_OVERLAP,
) -> list[dict[str, Any]]:
    """Chunk plain text (no block mapping). Returns list of {chunk_text, token_count}."""
    if strategy == ChunkingStrategy.paragraph:
        raw = _chunk_by_paragraph(text)
    elif strategy == ChunkingStrategy.sentence:
        raw = _chunk_by_sentence(text)
    elif strategy == ChunkingStrategy.fixed_size:
        raw = _chunk_by_fixed_size(text, chunk_size=chunk_size, overlap=overlap)
    else:
        raw = _chunk_by_paragraph(text)

    return [
        {"chunk_text": c, "token_count": estimate_tokens(c)}
        for c in _merge_and_split(raw, MIN_CHUNK_LENGTH, MAX_CHUNK_LENGTH)
    ]


# ═══════════════════════════════════════════════════════════════════
# Strategy: paragraph
# ═══════════════════════════════════════════════════════════════════


def _chunk_by_paragraph(text: str) -> list[str]:
    """Split text by blank-line (double-newline) boundaries."""
    paragraphs = re.split(r"\n\s*\n", text)
    return [p.strip() for p in paragraphs if p.strip()]


# ═══════════════════════════════════════════════════════════════════
# Strategy: sentence
# ═══════════════════════════════════════════════════════════════════


def _chunk_by_sentence(text: str) -> list[str]:
    """Split text on sentence-ending punctuation + optional whitespace."""
    parts = _SENTENCE_END.split(text)
    sentences: list[str] = []
    buf: list[str] = []
    for part in parts:
        if not part.strip():
            continue
        buf.append(part.strip())
        # Flush when we hit a sentence terminator
        if re.search(r"[。！？.!?]$", part.strip()):
            sentences.append(" ".join(buf))
            buf = []
    if buf:
        sentences.append(" ".join(buf))
    return sentences


# ═══════════════════════════════════════════════════════════════════
# Strategy: fixed_size
# ═══════════════════════════════════════════════════════════════════


def _chunk_by_fixed_size(text: str, *, chunk_size: int, overlap: int) -> list[str]:
    """Sliding window chunks of fixed character length with overlap."""
    if len(text) <= chunk_size:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start = end - overlap
    return chunks


# ═══════════════════════════════════════════════════════════════════
# Post-processing
# ═══════════════════════════════════════════════════════════════════


def _merge_and_split(chunks: list[str], min_len: int, max_len: int) -> list[str]:
    """Merge short adjacent chunks and split oversized ones."""
    if not chunks:
        return []

    # Merge short chunks into neighbours
    merged: list[str] = []
    pending = ""
    for c in chunks:
        combined = (pending + " " + c).strip() if pending else c
        if len(combined) < min_len:
            pending = combined
        else:
            merged.append(combined)
            pending = ""
    if pending:
        if merged:
            merged[-1] = (merged[-1] + " " + pending).strip()
        else:
            merged.append(pending)

    # Split oversized chunks
    result: list[str] = []
    for c in merged:
        if len(c) <= max_len:
            result.append(c)
            continue
        # Split at nearest paragraph/sentence boundary within max_len
        while len(c) > max_len:
            split_at = c.rfind("\n", 0, max_len)
            if split_at < min_len:
                split_at = c.rfind("。", 0, max_len)
            if split_at < min_len:
                split_at = c.rfind(". ", 0, max_len)
            if split_at < min_len:
                split_at = max_len
            result.append(c[:split_at].strip())
            c = c[split_at:].strip()
        if c:
            result.append(c)

    return result


def _resolve_block_id(char_pos: int, block_spans: list[tuple[UUID, int, int]]) -> UUID | None:
    """Find which block a character position belongs to."""
    for bid, start, end in block_spans:
        if start <= char_pos < end:
            return bid
    return None


def _post_process(
    raw_chunks: list[str],
    block_spans: list[tuple[UUID, int, int]],
) -> list[ChunkRecord]:
    """Convert raw text chunks to ChunkRecords with token count and block_id."""
    merged = _merge_and_split(raw_chunks, MIN_CHUNK_LENGTH, MAX_CHUNK_LENGTH)

    # Build cumulative positions for block-span mapping
    results: list[ChunkRecord] = []
    cursor = 0
    for i, text in enumerate(merged):
        # Find the block that contains the middle of this chunk
        mid = cursor + len(text) // 2
        block_id = _resolve_block_id(mid, block_spans)

        results.append(ChunkRecord(
            chunk_order=i,
            chunk_text=text,
            token_count=estimate_tokens(text),
            block_id=block_id,
            span_start=cursor,
            span_end=cursor + len(text),
        ))
        cursor += len(text) + 1  # for separator

    return results
