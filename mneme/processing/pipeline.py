"""Unified file processing pipeline — detect → parse → clean → chunk."""

from __future__ import annotations

from mneme.knowledge.chunking import ChunkingStrategy, chunk_text
from mneme.parsers import BlockDraft, detect_mime_type, get_parser
from mneme.processing.cleaner import clean_text
from mneme.security.file_encrypt import decrypt_file, is_encrypted


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
        if "一" <= ch <= "鿿" or "㐀" <= ch <= "䶿":
            if non_cjk_buffer:
                count += len("".join(non_cjk_buffer).split())
                non_cjk_buffer.clear()
            count += 1
        elif cat.startswith("L"):
            non_cjk_buffer.append(ch)
        else:
            if non_cjk_buffer:
                count += len("".join(non_cjk_buffer).split())
                non_cjk_buffer.clear()

    if non_cjk_buffer:
        count += len("".join(non_cjk_buffer).split())

    return count


def process_file(
    filename: str,
    content: bytes,
    *,
    key: bytes | None = None,
    chunk_size: int = 500,
    overlap: int = 50,
) -> dict:
    """Complete processing pipeline: detect type → parse → clean → chunk.

    Args:
        filename: File name (used for MIME detection).
        content: Raw file bytes (may be encrypted).
        key: Decryption key. If provided and content is encrypted, auto-decrypts.
        chunk_size: Target chunk size in characters (default 500).
        overlap: Overlap between chunks (default 50).

    Returns:
        dict with keys: chunks, word_count, file_type, filename, size.

    Raises:
        ValueError: Unsupported file type or decryption failure.
    """
    # 0. Auto-decrypt if encrypted and key provided
    if key and is_encrypted(content):
        content = decrypt_file(content, key)

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

    # 6. Word count (CJK-aware: count CJK chars individually + space-separated words)
    word_count = _count_words(full_text)

    return {
        "chunks": chunks,
        "word_count": word_count,
        "file_type": mime_type,
        "filename": filename,
        "size": len(content),
    }
