"""Unified file processing pipeline — detect → parse → clean → chunk."""

from __future__ import annotations

from mneme.knowledge.chunking import ChunkingStrategy, chunk_text
from mneme.parsers import BlockDraft, detect_mime_type, get_parser
from mneme.processing.cleaner import clean_text


def process_file(
    filename: str,
    content: bytes,
    *,
    chunk_size: int = 500,
    overlap: int = 50,
) -> dict:
    """Complete processing pipeline: detect type → parse → clean → chunk.

    Args:
        filename: File name (used for MIME detection).
        content: Raw file bytes.
        chunk_size: Target chunk size in characters (default 500).
        overlap: Overlap between chunks (default 50).

    Returns:
        dict with keys: chunks, word_count, file_type, filename, size.

    Raises:
        ValueError: Unsupported file type.
    """
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
        cleaned = clean_text(block.content_markdown)
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

    chunk_results = chunk_text(
        full_text,
        strategy=ChunkingStrategy.fixed_size,
        chunk_size=chunk_size,
        overlap=overlap,
    )
    chunks = [c["chunk_text"] for c in chunk_results]

    # 6. Word count (character count for Chinese)
    word_count = sum(len(c.replace(" ", "").replace("\n", "")) for c in chunks)

    return {
        "chunks": chunks,
        "word_count": word_count,
        "file_type": mime_type,
        "filename": filename,
        "size": len(content),
    }
