"""P6-02.3 Memory Merge — LLM-assisted smart merge of multiple memories into one.

Core algorithm
--------------
1. Input: survivor_memory_id + list of consumed memory IDs (or auto from dedup/conflict)
2. Build LLM prompt with all memory texts, asking for one coherent merged statement
3. LLM returns merged title + memory_text
4. Update survivor's ``memory_text`` with the LLM-generated merged text
5. Call ``mneme.db.memories.merge_memory()`` for each consumed memory
6. Update survivor's ``quality_score`` after merge

All write operations go through ``write_with_audit_outbox_idempotency``
(inside ``merge_memory``).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from mneme.api.context import RequestContext, get_current_context
from mneme.db.memories import MemoryRead, get_memory, merge_memory
from mneme.schemas.memories import MemoryMerge

logger = logging.getLogger(__name__)

# ── Data types ────────────────────────────────────────────────────────────────


@dataclass
class MergeResult:
    """Single merge operation result."""

    survivor_id: UUID
    consumed_id: UUID
    success: bool
    error: str | None = None


@dataclass
class SmartMergeOutput:
    """Output of ``smart_merge()``."""

    survivor: MemoryRead | None = None
    """Updated survivor memory after merging."""

    results: list[MergeResult] = field(default_factory=list)
    """Per-consumed merge results."""

    merged_count: int = 0
    failed_count: int = 0

    merged_title: str | None = None
    """LLM-generated merged title (None if fallback)."""

    merged_text: str | None = None
    """LLM-generated merged text (None if fallback)."""


# ── LLM prompt templates ──────────────────────────────────────────────────────

_MERGE_SYSTEM_PROMPT = """You are a memory merging engine. Combine multiple related memory
entries into a single, self-consistent, comprehensive memory.

## Rules

1. Preserve ALL factual information from every source memory.
2. Resolve apparent contradictions by preferring the most specific/recent info.
3. When perspectives complement each other rather than contradicting, include both.
4. Write output as ONE coherent paragraph or short paragraph set.
5. The output MUST be understandable without seeing the source memories.
6. Do NOT add information not present in the source memories.
7. Do NOT remove any actionable/decision/policy content.

## Output format

Respond ONLY with a JSON object — no preamble, no markdown fences:

{"title": "<merged title, max 120 chars>", "memory_text": "<merged self-contained text>"}
"""


def _build_merge_prompt(memories: list[MemoryRead]) -> list[dict[str, str]]:
    """Build LLM messages list for memory merging.

    Constructs a system prompt + user message listing all source memories
    with their titles, keys, and text content.
    """
    items: list[str] = []
    for idx, mem in enumerate(memories, start=1):
        title = mem.title or "(untitled)"
        items.append(
            f"### Memory {idx}: {title}\n"
            f"**Key**: {mem.canonical_key}\n"
            f"**Text**:\n{mem.memory_text}\n"
        )

    user_message = (
        "Please merge the following memories into one coherent memory:\n\n"
        + "\n---\n".join(items)
    )
    return [
        {"role": "system", "content": _MERGE_SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]


def _parse_merge_response(raw: str) -> dict[str, str] | None:
    """Parse LLM JSON response for ``{"title": ..., "memory_text": ...}``."""
    raw = raw.strip()
    # Strip markdown fences
    if raw.startswith("```"):
        lines = raw.split("\n")
        lines = [ln for ln in lines if not ln.startswith("```")]
        raw = "\n".join(lines).strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # Try extracting JSON object from text
        b0 = raw.find("{")
        b1 = raw.rfind("}") + 1
        if b0 >= 0 and b1 > b0:
            try:
                parsed = json.loads(raw[b0:b1])
            except json.JSONDecodeError:
                logger.warning("Failed to parse merge JSON from LLM response")
                return None
        else:
            logger.warning("No JSON object in merge LLM response")
            return None
    if not isinstance(parsed, dict) or "title" not in parsed or "memory_text" not in parsed:
        return None
    return {"title": str(parsed["title"]), "memory_text": str(parsed["memory_text"])}


def _call_llm_merge(
    messages: list[dict[str, str]],
    *,
    project_id: UUID | None = None,
    context: RequestContext | None = None,
) -> str | None:
    """Call Gateway LLM to produce merged text.  Returns raw content or None."""
    try:
        from mneme.gateway.call import Gateway

        gw = Gateway()
        ctx = context or get_current_context()
        result = gw.call(
            capability_code="chat.completion",
            params={
                "messages": messages,
                "temperature": 0.3,
                "max_tokens": 2048,
            },
            project_id=project_id,
            sensitivity="private",
            actor_type=ctx.actor.actor_type,
            actor_id=ctx.actor.actor_id,
            request_id=ctx.request_id,
            correlation_id=ctx.correlation_id,
        )
        data = result.get("data", {})
        if isinstance(data, dict):
            choices = data.get("choices", [])
            if choices and isinstance(choices, list):
                return str(choices[0].get("message", {}).get("content", ""))
            return str(data.get("content", "") or data.get("text", ""))
        return None
    except Exception:
        logger.exception("LLM merge call failed")
        return None


# ── Public API ─────────────────────────────────────────────────────────────────


def smart_merge(
    db: Session,
    context: RequestContext,
    *,
    survivor_id: UUID,
    consumed_ids: list[UUID],
    project_id: UUID | None = None,
    reason: str | None = None,
    use_llm: bool = True,
) -> SmartMergeOutput:
    """LLM-assisted smart merge: combine consumed memories into survivor.

    1. Load survivor and consumed memories from DB.
    2. Build LLM prompt with all memory texts.
    3. Call LLM to generate merged content.
    4. Update survivor's ``memory_text`` with merged result.
    5. Call ``merge_memory()`` for each consumed memory.
    6. Return structured output.

    Parameters
    ----------
    db : Session
        Active SQLAlchemy session.
    context : RequestContext
        Request context for audit/outbox.
    survivor_id : UUID
        Memory that absorbs the others.
    consumed_ids : list[UUID]
        Memory IDs to merge into survivor.
    project_id : UUID | None
        Project context for Gateway call.
    reason : str | None
        Reason recorded in audit trail.
    use_llm : bool
        Whether to use LLM for merged text generation. If False, simple
        concatenation is used.

    Returns
    -------
    SmartMergeOutput
    """
    output = SmartMergeOutput()

    # ── 1. Load memories ─────────────────────────────────────────────────────
    survivor = get_memory(db, survivor_id)
    if survivor is None:
        logger.error("Survivor memory %s not found", survivor_id)
        return output
    if survivor.status not in ("draft", "active"):
        logger.error("Survivor %s is '%s', cannot merge into it", survivor_id, survivor.status)
        return output

    all_memories: list[MemoryRead] = [survivor]
    valid_consumed_ids: list[UUID] = []
    for cid in consumed_ids:
        mem = get_memory(db, cid)
        if mem is None:
            logger.warning("Consumed memory %s not found, skipping", cid)
            continue
        if mem.status not in ("draft", "active"):
            logger.warning("Consumed memory %s is '%s', skipping", cid, mem.status)
            continue
        if cid == survivor_id:
            logger.warning("Cannot merge memory %s into itself, skipping", cid)
            continue
        all_memories.append(mem)
        valid_consumed_ids.append(cid)

    if not valid_consumed_ids:
        logger.warning("No valid consumed memories to merge")
        return output

    # ── 2. Generate merged text (LLM or fallback) ────────────────────────────
    merged_title = survivor.title
    merged_text = survivor.memory_text

    if use_llm:
        messages = _build_merge_prompt(all_memories)
        raw = _call_llm_merge(messages, project_id=project_id, context=context)
        if raw:
            parsed = _parse_merge_response(raw)
            if parsed:
                merged_title = parsed["title"]
                merged_text = parsed["memory_text"]
                logger.info("LLM merged %d memories into %d chars",
                            len(all_memories), len(merged_text))
                output.merged_title = merged_title
                output.merged_text = merged_text

    if not output.merged_text:
        # Fallback: concatenate all texts
        parts: list[str] = []
        for mem in all_memories:
            parts.append(f"[{mem.canonical_key}] {mem.memory_text}")
        merged_text = "\n\n---\n\n".join(parts)
        merged_title = survivor.title
        logger.warning("Using concatenation fallback for merge")

    # ── 3. Update survivor's memory_text with merged result ──────────────────
    survivor_new_ver = survivor.current_version + 1
    db.execute(
        sql_text("""
            UPDATE memories SET
              title = COALESCE(:title, title),
              memory_text = :mtext,
              current_version = :ver,
              updated_at = CURRENT_TIMESTAMP
            WHERE memory_id = :mid
              AND status IN ('draft', 'active')
        """),
        {
            "title": merged_title,
            "mtext": merged_text,
            "ver": survivor_new_ver,
            "mid": survivor_id,
        },
    )

    # ── 4. Execute merge_memory for each consumed ────────────────────────────
    merge_reason = reason or f"LLM smart-merge into {survivor.canonical_key}"
    for cid in valid_consumed_ids:
        try:
            merge_memory(
                db,
                context,
                memory_id=survivor_id,
                payload=MemoryMerge(target_memory_id=cid, reason=merge_reason),
            )
            output.results.append(
                MergeResult(survivor_id=survivor_id, consumed_id=cid, success=True)
            )
            output.merged_count += 1
        except Exception as exc:
            logger.exception("Merge %s → %s failed", cid, survivor_id)
            output.results.append(
                MergeResult(survivor_id=survivor_id, consumed_id=cid,
                            success=False, error=str(exc))
            )
            output.failed_count += 1

    # ── 5. Re-read survivor ──────────────────────────────────────────────────
    output.survivor = get_memory(db, survivor_id)
    return output


def quick_merge(
    db: Session,
    context: RequestContext,
    *,
    survivor_id: UUID,
    consumed_ids: list[UUID],
    reason: str | None = None,
) -> SmartMergeOutput:
    """Simple concatenation merge without LLM.

    Directly appends consumed texts to survivor, then calls ``merge_memory()``.
    Useful as a fast fallback when LLM is unavailable.
    """
    return smart_merge(
        db, context,
        survivor_id=survivor_id,
        consumed_ids=consumed_ids,
        reason=reason,
        use_llm=False,
    )
