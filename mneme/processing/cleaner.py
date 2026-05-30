"""Text cleaning utilities for the Mneme processing pipeline."""

import re

# Control character removal: keep tab(0x09), newline(0x0a), CR(0x0d),
# printable ASCII (0x20-0x7e), Latin-1 Supplement (0xa0-0xff),
# General Punctuation (0x2000-0x206f), Letterlike Symbols (0x2100-0x214f),
# CJK Symbols & Punctuation (0x3000-0x303f), CJK Unified Ideographs (0x3400-0x9fff),
# Halfwidth & Fullwidth Forms (0xff00-0xffef).
_RE_CONTROL = re.compile(r"[^\x09\x0a\x0d\x20-\x7e\xa0-\xff -⁯℀-⅏　-〿㐀-鿿＀-￯]")

# Consolidate multiple spaces/tabs into a single space.
_RE_MULTI_SPACE = re.compile(r"[ \t]{2,}")

# Merge 3+ consecutive newlines into exactly 2 (a blank line).
_RE_MULTI_NEWLINE = re.compile(r"\n{3,}")

# Page header/footer patterns (applied with re.MULTILINE).
_RE_PAGE_CN = re.compile(r"^\s*第\s*\d+\s*页.*$", re.MULTILINE)
_RE_PAGE_DASH = re.compile(r"^\s*-\s*\d+\s*-\s*$", re.MULTILINE)
_RE_PAGE_EN = re.compile(r"^\s*Page\s+\d+.*$", re.MULTILINE | re.IGNORECASE)


def clean_text(text: str) -> str:
    """Clean raw extracted text.

    Pipeline (order matters):
    0. Normalize line endings (CRLF/CR → LF)
    1. Remove garbled / control characters
    2. Consolidate whitespace (spaces & tabs) → single space
    3. Merge multiple empty lines → single newline (double newline)
    4. Strip page header/footer patterns
    5. Re-merge empty lines created by footer removal
    6. Strip leading/trailing whitespace
    """
    # 0. Normalize line endings: CRLF → LF, standalone CR → LF
    text = text.replace("\r\n", "\n").replace("\r", "\n")

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

    # 5. Re-merge empty lines created by footer removal
    text = _RE_MULTI_NEWLINE.sub("\n\n", text)

    # 6. Final strip
    text = text.strip()

    return text
