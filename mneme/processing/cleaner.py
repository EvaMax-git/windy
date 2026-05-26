"""Text cleaning utilities for the Mneme processing pipeline."""

import re

# Control character removal: keep tab(0x09), newline(0x0a), CR(0x0d),
# printable ASCII (0x20-0x7e), CJK unified ideographs (一-鿿),
# and fullwidth/special forms.
_RE_CONTROL = re.compile(r"[^\x09\x0a\x0d\x20-\x7e -⁯㐀-鿿　-〿＀-￯]")

# Consolidate multiple spaces/tabs into a single space.
_RE_MULTI_SPACE = re.compile(r"[ \t]{2,}")

# Merge 3+ consecutive newlines into exactly 2 (a blank line).
_RE_MULTI_NEWLINE = re.compile(r"\n{3,}")

# Page header/footer patterns (applied with re.MULTILINE).
_RE_PAGE_CN = re.compile(r"^\s*第\s*\d+\s*页.*$", re.MULTILINE)
_RE_PAGE_DASH = re.compile(r"^\s*-\s*\d+\s*-\s*$", re.MULTILINE)
_RE_PAGE_EN = re.compile(r"^\s*Page\s+\d+.*$", re.MULTILINE)


def clean_text(text: str) -> str:
    """Clean raw extracted text.

    Pipeline (order matters):
    1. Remove garbled / control characters
    2. Consolidate whitespace (spaces & tabs) → single space
    3. Merge multiple empty lines → single newline (double newline)
    4. Strip page header/footer patterns
    5. Strip leading/trailing whitespace
    """
    # 1. Remove control / garbled characters
    text = _RE_CONTROL.sub("", text)

    # 2. Consolidate multiple spaces/tabs
    text = _RE_MULTI_SPACE.sub(" ", text)

    # 3. Merge multiple empty lines
    text = _RE_MULTI_NEWLINE.sub("\n\n", text)

    # 4. Remove page number patterns
    text = _RE_PAGE_CN.sub("", text)
    text = _RE_PAGE_DASH.sub("", text)
    text = _RE_PAGE_EN.sub("", text)

    # 5. Final strip
    text = text.strip()

    return text
