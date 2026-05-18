"""P4-09 LLM Extract — Prompt builder and response parser for Memory Extract Pipeline.

This module builds the system/user prompt for the LLM extraction call and
parses the structured JSON response into candidate memory records.

Design
------
* **Prompt**: structured system prompt instructing the LLM to extract discrete
  memory-worthy facts/knowledge from conversation messages or raw events.
* **Output format**: JSON with ``candidates[]``, each containing title, text,
  confidence, and optional evidence_spans.
* **Fallback**: If JSON parsing fails, returns an empty candidate list.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from mneme.memory.evidence_parser import EvidenceSpan, parse_evidence_spans

logger = logging.getLogger(__name__)


# ── Data types ──────────────────────────────────────────────────────────────


@dataclass
class ExtractedCandidate:
    """A single memory candidate extracted by the LLM."""

    title: str = ""
    """Short title summarizing the memory (max ~120 chars)."""

    candidate_text: str = ""
    """The full memory text — a self-contained statement of fact/knowledge."""

    confidence_score: float = 0.5
    """LLM-assigned confidence (0.0–1.0)."""

    evidence_spans: list[EvidenceSpan] = field(default_factory=list)
    """Source text spans that support this candidate."""


@dataclass
class ExtractResult:
    """Result of calling the extraction LLM."""

    candidates: list[ExtractedCandidate] = field(default_factory=list)
    """Extracted memory candidates."""

    raw_response: str = ""
    """Raw LLM response text (for audit/debug)."""

    parse_error: str | None = None
    """Error message if JSON parsing failed."""


# ── Prompt templates ────────────────────────────────────────────────────────

_EXTRACT_SYSTEM_PROMPT = """You are a memory extraction engine. Your task is to read a conversation message
and extract discrete, self-contained pieces of knowledge that should be remembered.

## What to extract

Extract facts, decisions, preferences, constraints, plans, or knowledge that:
1. Is a coherent, standalone statement (not dependent on conversational context).
2. Has lasting relevance beyond the current conversation.
3. Can be understood without seeing the rest of the conversation.

## What NOT to extract

Do NOT extract:
- Greetings, small talk, acknowledgements ("OK", "thanks", "got it").
- Redundant repetitions of already-stated facts.
- Incomplete or ambiguous fragments.
- Personal opinions stated as fleeting remarks.

## Output format

Respond ONLY with a JSON object — no preamble, no markdown fences:

{
  "candidates": [
    {
      "title": "<short title, max 120 chars>",
      "text": "<complete self-contained statement>",
      "confidence": <float 0.0 to 1.0>,
      "evidence_spans": [
        {
          "span_start": <int, char offset in source>,
          "span_end": <int, char offset in source>,
          "text_fragment": "<the cited text>",
          "confidence": <float 0.0 to 1.0>
        }
      ]
    }
  ]
}

If there is nothing worth remembering, return: {"candidates": []}

## Guidelines

- Each candidate should capture ONE distinct fact/decision/knowledge item.
- confidence: 0.9+ for clear factual statements, 0.5-0.89 for inferences, <0.5 for vague.
- evidence_spans MUST use character offsets from the source text. Omit if unclear.
- Title should be a short noun phrase summarizing the memory.
"""


def build_extract_prompt(
    *,
    source_text: str,
    source_type: str = "message",
    conversation_context: str = "",
) -> list[dict[str, str]]:
    """Build the LLM messages list for memory extraction.

    Parameters
    ----------
    source_text : str
        The message or event text to extract from.
    source_type : str
        ``"message"`` or ``"raw_event"`` — influences prompt framing.
    conversation_context : str
        Optional preceding conversation text for context (not extracted from).

    Returns
    -------
    list[dict]
        OpenAI-compatible messages list: system + user.
    """
    system = _EXTRACT_SYSTEM_PROMPT

    user_parts = [f"Source type: {source_type}"]
    if conversation_context:
        user_parts.append(f"\nConversation context (for understanding, not extraction):\n---\n{conversation_context}\n---")
    user_parts.append(f"\nMessage to extract from:\n---\n{source_text}\n---\nExtract memory candidates:")

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n".join(user_parts)},
    ]


# ── Response parser ─────────────────────────────────────────────────────────


def parse_extract_response(
    raw_text: str,
    *,
    source_text: str = "",
) -> ExtractResult:
    """Parse the LLM JSON response into structured candidates.

    Parameters
    ----------
    raw_text : str
        The raw response text from the LLM.
    source_text : str
        The original source text (for evidence span validation).

    Returns
    -------
    ExtractResult
        Parsed candidates with any parse errors noted.
    """
    result = ExtractResult(raw_response=raw_text)

    # Strip markdown code fences if present
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        # Remove ```json ... ``` or ``` ... ```
        cleaned = cleaned.removeprefix("```json").removeprefix("```").strip()
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].strip()

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        logger.warning("Failed to parse LLM extract JSON: %s", exc)
        result.parse_error = f"JSON decode error: {exc}"
        return result

    if not isinstance(data, dict):
        result.parse_error = "Top-level response is not a JSON object"
        return result

    raw_candidates = data.get("candidates", [])
    if not isinstance(raw_candidates, list):
        result.parse_error = "'candidates' field is not a list"
        return result

    for raw in raw_candidates:
        if not isinstance(raw, dict):
            continue

        title = str(raw.get("title", "")).strip()
        text = str(raw.get("text", raw.get("candidate_text", ""))).strip()

        if not text:
            continue  # skip empty candidates

        try:
            confidence = float(raw.get("confidence", 0.5))
        except (ValueError, TypeError):
            confidence = 0.5
        confidence = max(0.0, min(1.0, confidence))

        evidence_spans = parse_evidence_spans(
            raw.get("evidence_spans"),
            source_text=source_text,
        )

        result.candidates.append(
            ExtractedCandidate(
                title=title,
                candidate_text=text,
                confidence_score=confidence,
                evidence_spans=evidence_spans,
            )
        )

    return result
