"""Parser facade — unified file processing interface.

Provides simple top-level functions for file processing pipeline:
    process_file → full pipeline (detect → parse → clean → chunk)
    parse_file   → extract text only (no cleaning/chunking)
    clean_text   → clean raw text
    chunk_text   → split text into chunks
"""

from __future__ import annotations

from pathlib import Path

from mneme.knowledge.chunking import ChunkingStrategy, chunk_text as _chunk_text
from mneme.parsers import BlockDraft, detect_mime_type, get_parser
from mneme.processing.cleaner import clean_text as _clean_text


def process_file(file_path: str, *, chunk_size: int = 500, overlap: int = 50) -> dict:
    """Full processing pipeline: detect → parse → clean → chunk.

    Args:
        file_path: Path to the file to process.
        chunk_size: Target chunk size in characters (default 500).
        overlap: Overlap between chunks (default 50).

    Returns:
        dict with keys: chunks, word_count, file_type, filename, size.

    Raises:
        ValueError: Unsupported file type or empty file.
        FileNotFoundError: File does not exist.
        TypeError: If file_path is None.
    """
    if file_path is None:
        raise TypeError("file_path must not be None")

    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"文件不存在: {file_path}")

    content = path.read_bytes()
    filename = path.name

    # 1. Detect MIME type
    mime_type = detect_mime_type(filename, content)

    # 2. Get parser
    parser = get_parser(mime_type)
    if parser is None:
        raise ValueError(f"不支持的文件类型: {mime_type}")

    # 3. Parse → BlockDraft list
    blocks: list[BlockDraft] = parser(content)

    # 4. Clean each block
    cleaned_parts: list[str] = []
    for block in blocks:
        cleaned = _clean_text(block.content_markdown)
        if cleaned:
            cleaned_parts.append(cleaned)

    # 5. Merge and chunk
    full_text = "\n\n".join(cleaned_parts)
    if not full_text.strip():
        return {
            "chunks": [],
            "word_count": 0,
            "file_type": mime_type,
            "filename": filename,
            "size": len(content),
        }

    chunk_results = _chunk_text(
        full_text,
        strategy=ChunkingStrategy.fixed_size,
        chunk_size=chunk_size,
        overlap=overlap,
    )
    chunks = [c["chunk_text"] for c in chunk_results]

    # 6. Word count (CJK-aware: count CJK chars individually + space-separated words)
    word_count = _count_words(full_text)

    return {
        "chunks": chunks,
        "word_count": word_count,
        "file_type": mime_type,
        "filename": filename,
        "size": len(content),
    }


def _count_words(text: str) -> int:
    """Count words in text, handling CJK characters appropriately.

    CJK characters are counted individually (each is a "word").
    Non-CJK text is split by whitespace.
    """
    import unicodedata

    count = 0
    non_cjk_buffer: list[str] = []

    for ch in text:
        cat = unicodedata.category(ch)
        # CJK Unified Ideographs and common CJK ranges
        if "一" <= ch <= "鿿" or "㐀" <= ch <= "䶿":
            # Flush non-CJK buffer
            if non_cjk_buffer:
                count += len("".join(non_cjk_buffer).split())
                non_cjk_buffer.clear()
            count += 1
        elif cat.startswith("L"):  # Other letters
            non_cjk_buffer.append(ch)
        else:
            # Whitespace/punctuation — flush buffer
            if non_cjk_buffer:
                count += len("".join(non_cjk_buffer).split())
                non_cjk_buffer.clear()

    # Flush remaining
    if non_cjk_buffer:
        count += len("".join(non_cjk_buffer).split())

    return count


def parse_file(file_path: str) -> str:
    """Extract text from a file without cleaning or chunking.

    Args:
        file_path: Path to the file to parse.

    Returns:
        Extracted text as a single string.

    Raises:
        ValueError: Unsupported file type.
        FileNotFoundError: File does not exist.
        TypeError: If file_path is None.
    """
    if file_path is None:
        raise TypeError("file_path must not be None")

    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"文件不存在: {file_path}")

    content = path.read_bytes()
    filename = path.name

    mime_type = detect_mime_type(filename, content)
    parser = get_parser(mime_type)
    if parser is None:
        raise ValueError(f"不支持的文件类型: {mime_type}")

    blocks: list[BlockDraft] = parser(content)
    return "\n\n".join(block.content_markdown for block in blocks)


def clean_text(text: str) -> str:
    """Clean raw extracted text.

    Removes control characters, normalizes whitespace, strips page
    headers/footers, and merges multiple empty lines.

    Args:
        text: Raw text to clean.

    Returns:
        Cleaned text.

    Raises:
        TypeError: If text is None.
    """
    if text is None:
        raise TypeError("text must not be None")
    return _clean_text(text)


def chunk_text(text: str, size: int = 500, overlap: int = 50) -> list[str]:
    """Split text into fixed-size chunks with overlap.

    Args:
        text: Text to chunk.
        size: Target chunk size in characters (default 500).
        overlap: Overlap between chunks (default 50).

    Returns:
        List of chunk strings.

    Raises:
        TypeError: If text is None.
        ValueError: If overlap >= size.
    """
    if text is None:
        raise TypeError("text must not be None")
    if overlap >= size:
        raise ValueError(f"overlap ({overlap}) must be less than size ({size})")
    if not text.strip():
        return []

    chunk_results = _chunk_text(
        text,
        strategy=ChunkingStrategy.fixed_size,
        chunk_size=size,
        overlap=overlap,
    )
    return [c["chunk_text"] for c in chunk_results]
