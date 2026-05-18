"""jieba segmentation wrapper with fallback for Chinese FTS.

Usage::

    from mneme.knowledge.jieba_segment import segment, segment_query

    segmented = segment(raw_text)       # for indexing
    query     = segment_query(raw_q)    # for search
"""
from __future__ import annotations

try:
    import jieba
    _JIEBA = True
except ImportError:
    _JIEBA = False


def segment(text: str) -> str:
    """Segment Chinese text with jieba. Returns space-separated words.

    Falls back to the raw text if jieba is not installed.
    """
    if not _JIEBA or not text:
        return text
    words = jieba.cut(text.strip())
    return " ".join(words)


def segment_query(query: str) -> str:
    """Segment a search query for use with plainto_tsquery."""
    return segment(query)


def is_available() -> bool:
    return _JIEBA


def check_segmentation_quality(text: str) -> dict:
    """Quick quality check: single-char token ratio.

    High ratio (>40% single-char tokens) suggests poor segmentation.
    """
    if not _JIEBA or not text:
        return {"available": _JIEBA, "single_char_ratio": None}

    words = list(jieba.cut(text.strip()))
    single = sum(1 for w in words if len(w) == 1)
    return {
        "available": True,
        "total_tokens": len(words),
        "single_char_tokens": single,
        "single_char_ratio": round(single / max(len(words), 1), 3),
    }
