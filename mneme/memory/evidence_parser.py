"""P4-09 Evidence spans parser — extract and validate ``evidence_spans`` from LLM output.

LLM Extract Pipeline outputs optional ``evidence_spans`` that link each candidate
memory back to specific text fragments in the source message/event.

Span format
-----------
Each span annotates a source text region:

.. code-block:: json

    {
      "span_start": 0,
      "span_end": 48,
      "text_fragment": "We decided to use PostgreSQL as our primary database",
      "confidence": 0.92
    }

Validation
----------
* ``span_start`` and ``span_end`` are character offsets into the source text.
* ``text_fragment`` should match ``source_text[span_start:span_end]`` (best-effort).
* ``confidence`` is optional (default 1.0).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class EvidenceSpan:
    """A single evidence span linking a memory candidate to source text."""

    span_start: int
    """Character offset (inclusive) of the start of the cited text."""

    span_end: int
    """Character offset (exclusive) of the end of the cited text."""

    text_fragment: str = ""
    """The cited text snippet from the source."""

    confidence: float = 1.0
    """Confidence score for this specific span (0.0–1.0)."""

    def to_source_span_json(self) -> dict[str, Any]:
        """Serialize to the JSONB format expected by ``memory_sources.source_span``."""
        return {
            "span_start": self.span_start,
            "span_end": self.span_end,
            "text_snippet": self.text_fragment,
        }


def parse_evidence_spans(
    raw_spans: list[dict[str, Any]] | None,
    *,
    source_text: str = "",
) -> list[EvidenceSpan]:
    """Parse and validate evidence spans from LLM output.

    Parameters
    ----------
    raw_spans : list[dict] or None
        Raw span dictionaries from the LLM response JSON.
    source_text : str
        The original source text for cross-validating fragments (best-effort).

    Returns
    -------
    list[EvidenceSpan]
        Validated evidence spans (empty list if raw_spans is None or malformed).
    """
    if not raw_spans or not isinstance(raw_spans, list):
        return []

    spans: list[EvidenceSpan] = []

    for raw in raw_spans:
        if not isinstance(raw, dict):
            continue

        try:
            span_start = int(raw.get("span_start", 0))
            span_end = int(raw.get("span_end", 0))
        except (ValueError, TypeError):
            continue

        if span_start < 0 or span_end < 0 or span_end <= span_start:
            continue

        text_fragment = str(raw.get("text_fragment", ""))

        # Cross-validate fragment against source text (best-effort)
        if source_text and text_fragment:
            actual = source_text[span_start:span_end]
            if actual != text_fragment:
                # Use the actual source text fragment instead
                text_fragment = actual

        confidence = float(raw.get("confidence", 1.0))
        confidence = max(0.0, min(1.0, confidence))

        spans.append(
            EvidenceSpan(
                span_start=span_start,
                span_end=span_end,
                text_fragment=text_fragment,
                confidence=confidence,
            )
        )

    # Deduplicate overlapping spans: keep highest confidence
    spans.sort(key=lambda s: (s.span_start, -s.confidence))
    merged: list[EvidenceSpan] = []
    for span in spans:
        if merged and span.span_start < merged[-1].span_end:
            if span.confidence > merged[-1].confidence:
                merged[-1] = span
        else:
            merged.append(span)

    return merged
