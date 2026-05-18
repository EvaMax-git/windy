"""Token estimation utilities for knowledge text.

Used by both block creation and chunking to calculate token counts.
"""

from __future__ import annotations

import re

_MARKDOWN_PAT = re.compile(r"[*_~`#>|\[\]()\-!\\]+")


def strip_markdown(text: str) -> str:
    """Remove basic markdown syntax to produce plain text."""
    cleaned = _MARKDOWN_PAT.sub(" ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def estimate_tokens(text: str) -> int:
    """Crude token estimation: CJK chars * 0.5 + non-CJK words * 1.3."""
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿" or "　" <= ch <= "〿")
    non_cjk = len(text) - cjk
    words = len(text.split()) if cjk == 0 else max(1, len(re.findall(r"[a-zA-Z0-9]+", text)))
    return max(1, int(cjk * 0.5 + words * 1.3))
