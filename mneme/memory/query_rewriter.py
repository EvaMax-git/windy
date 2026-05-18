"""Query Rewriter — resolve vague temporal / demonstrative references in search queries.

Resolves references like:
* "上次那个bug" ("that bug from last time") → specific bug description
* "昨天的项目" ("yesterday's project") → specific project name
* "那个agent" ("that agent") → specific agent name
* "之前的会议" ("previous meeting") → specific meeting title

The rewriter:
1. Gathers context from recent conversations, memories, and agents
2. Builds an LLM prompt with query + context
3. Calls the LLM via Gateway
4. Returns the rewritten, specific query

Design
------
* Lightweight — single LLM call with minimal token usage
* Degrade gracefully — if LLM call fails, falls back to original query
* Optional — controlled by ``rewrite`` query parameter in global_search
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from mneme.api.context import RequestContext
from mneme.gateway.call import Gateway, GatewayError

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────────

_MAX_CONTEXT_CHARS = 3000  # max total characters for context in prompt
_RECENT_DAYS = 14          # how many days back to look for recent conversations/memories
_CONTEXT_DOC_LIMIT = 20    # max rows per context category

# ── Data types ───────────────────────────────────────────────────────────────────


@dataclass
class RewriteResult:
    """Result of query rewriting."""

    original_query: str
    """The user's original search query."""

    rewritten_query: str
    """The rewritten, specific query (falls back to original if no rewrite needed)."""

    is_rewritten: bool = False
    """True if the query was actually modified."""

    confidence: float = 0.0
    """LLM-assigned confidence (0.0–1.0)."""

    explanation: str = ""
    """Brief explanation of what was resolved."""

    context_summary: str = ""
    """Summary of context used for rewriting (for audit/debug)."""

    raw_response: str = ""
    """Raw LLM response text."""

    parse_error: str | None = None
    """Error message if JSON parsing failed."""


# ── Prompt templates ─────────────────────────────────────────────────────────────

_REWRITE_SYSTEM_PROMPT = """You are a query rewriting engine. Your task is to resolve vague or ambiguous
references in a user's search query into specific, concrete search terms.

## What to rewrite

The user may use vague references like:
- "上次那个bug" / "那个bug" → resolve to the specific bug mentioned in context
- "昨天的项目" / "之前的项目" → resolve to the specific project name
- "那个agent" / "那个机器人" → resolve to the specific agent name
- "之前的会议" / "上次的讨论" → resolve to the specific meeting/discussion title
- "那个问题" / "那个功能" → resolve to the specific issue/feature from context

## Rules

1. ONLY rewrite when you have sufficient evidence from the context.
2. Do NOT add information that is not supported by the context.
3. Do NOT hallucinate details. If you're unsure, return the original query unchanged.
4. The rewritten query should be in Chinese if the original is in Chinese.
5. Keep the rewritten query concise — usually 2-15 words.
6. Add specific keywords, dates, names from context to make the query concrete.

## Output format

Respond ONLY with a JSON object — no preamble, no markdown fences:

{
  "rewritten_query": "<the specific rewritten query, or the original if no rewrite needed>",
  "is_rewritten": <true or false>,
  "confidence": <float 0.0 to 1.0>,
  "explanation": "<brief explanation of what was resolved>"
}

If the query is already specific and doesn't need rewriting, set is_rewritten=false
and return the original query as rewritten_query.
"""


def build_rewrite_prompt(
    *,
    query: str,
    context_text: str,
) -> list[dict[str, str]]:
    """Build the LLM messages list for query rewriting.

    Parameters
    ----------
    query : str
        The user's search query (may contain vague references).
    context_text : str
        Pre-assembled context text from recent conversations, memories, and agents.

    Returns
    -------
    list[dict]
        OpenAI-compatible messages list: system + user.
    """
    user_parts = [
        f"User query: {query}",
        "",
        "Context (recent conversations, memories, and agents in this project):",
        "---",
        context_text,
        "---",
        "",
        "Rewrite the query to resolve any vague references based on the context above.",
        "If the query is already specific, return it unchanged.",
    ]

    return [
        {"role": "system", "content": _REWRITE_SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(user_parts)},
    ]


def parse_rewrite_response(
    raw_text: str,
    *,
    original_query: str,
) -> RewriteResult:
    """Parse the LLM JSON response into a :class:`RewriteResult`.

    Parameters
    ----------
    raw_text : str
        The raw response text from the LLM.
    original_query : str
        The original user query (used as fallback).

    Returns
    -------
    RewriteResult
        Parsed result with the rewritten query or fallback.
    """
    result = RewriteResult(
        original_query=original_query,
        rewritten_query=original_query,
        raw_response=raw_text,
    )

    # Strip markdown code fences if present
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.removeprefix("```json").removeprefix("```").strip()
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].strip()

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        logger.warning("Failed to parse rewrite LLM JSON: %s", exc)
        result.parse_error = f"JSON decode error: {exc}"
        return result

    if not isinstance(data, dict):
        result.parse_error = "Top-level response is not a JSON object"
        return result

    rewritten = str(data.get("rewritten_query", "")).strip()
    if rewritten and rewritten != original_query:
        result.rewritten_query = rewritten
        result.is_rewritten = True
    else:
        result.rewritten_query = original_query
        result.is_rewritten = False

    try:
        result.confidence = float(data.get("confidence", 0.0))
    except (ValueError, TypeError):
        result.confidence = 0.0
    result.confidence = max(0.0, min(1.0, result.confidence))

    result.explanation = str(data.get("explanation", ""))

    return result


# ── Context gathering ────────────────────────────────────────────────────────────


def _recent_cutoff_date(db: Session, days: int = _RECENT_DAYS) -> str:
    """Return a recent cutoff date string compatible with the current DB dialect."""
    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        return cutoff.strftime("%Y-%m-%d %H:%M:%S")
    else:
        return cutoff.isoformat()


def gather_context_for_rewrite(
    db: Session,
    *,
    project_id: UUID | None,
) -> str:
    """Gather contextual data for the query rewriter from the database.

    Fetches:
    * Recent conversations (titles + latest message snippets)
    * Recent active memories (titles + text previews)
    * Active agents (names + descriptions)

    The result is a plain-text summary string for inclusion in the LLM prompt.

    Parameters
    ----------
    db : Session
        Active database session.
    project_id : UUID or None
        Filter context to a specific project. If None, gathers across all projects.

    Returns
    -------
    str
        Formatted context text string. May be empty if no context is available.
    """
    parts: list[str] = []
    total_chars = 0
    cutoff_date = _recent_cutoff_date(db)

    # ── 1. Recent conversations ──────────────────────────────────────────────
    conv_sql = text("""
        SELECT c.title, c.conversation_type, c.started_at,
               (SELECT m.content_text
                FROM messages m
                WHERE m.conversation_id = c.conversation_id
                ORDER BY m.message_time DESC
                LIMIT 1
               ) AS latest_message
        FROM conversations c
        WHERE c.conversation_status = 'active'
          AND (:project_id IS NULL OR c.project_id = :project_id)
          AND c.started_at >= :cutoff_date
        ORDER BY c.started_at DESC
        LIMIT :limit
    """)

    conv_rows = db.execute(
        conv_sql,
        {
            "project_id": project_id,
            "cutoff_date": cutoff_date,
            "limit": _CONTEXT_DOC_LIMIT,
        },
    ).mappings().all()

    if conv_rows:
        conv_lines = ["[Recent Conversations]"]
        for row in conv_rows:
            title = (row["title"] or "Untitled")[:120]
            conv_type = row["conversation_type"] or "chat"
            started = row["started_at"]
            date_str = started.strftime("%Y-%m-%d") if started else "unknown"
            latest = (row["latest_message"] or "")[:200]
            line = f"- [{date_str}] [{conv_type}] {title}"
            if latest:
                line += f" (latest: {latest})"
            conv_lines.append(line)
            total_chars += len(line)
            if total_chars > _MAX_CONTEXT_CHARS:
                break
        parts.append("\n".join(conv_lines))

    # ── 2. Recent active memories ────────────────────────────────────────────
    if total_chars < _MAX_CONTEXT_CHARS:
        mem_sql = text("""
            SELECT title, memory_text, canonical_key, updated_at
            FROM memories
            WHERE status = 'active'
              AND (:project_id IS NULL OR project_id = :project_id)
            ORDER BY updated_at DESC
            LIMIT :limit
        """)

        mem_rows = db.execute(
            mem_sql,
            {
                "project_id": project_id,
                "limit": _CONTEXT_DOC_LIMIT,
            },
        ).mappings().all()

        if mem_rows:
            mem_lines = ["[Recent Memories]"]
            for row in mem_rows:
                title = (row["title"] or row["canonical_key"] or "Untitled")[:120]
                text_preview = (row["memory_text"] or "")[:150]
                updated = row["updated_at"]
                date_str = updated.strftime("%Y-%m-%d") if updated else ""
                line = f"- [{date_str}] {title}"
                if text_preview:
                    line += f": {text_preview}"
                mem_lines.append(line)
                total_chars += len(line)
                if total_chars > _MAX_CONTEXT_CHARS:
                    break
            parts.append("\n".join(mem_lines))

    # ── 3. Active agents ─────────────────────────────────────────────────────
    if total_chars < _MAX_CONTEXT_CHARS:
        agent_sql = text("""
            SELECT name, description, agent_code
            FROM agents
            WHERE status = 'active'
              AND (:project_id IS NULL OR project_id = :project_id)
            ORDER BY name ASC
            LIMIT :limit
        """)

        agent_rows = db.execute(
            agent_sql,
            {
                "project_id": project_id,
                "limit": _CONTEXT_DOC_LIMIT,
            },
        ).mappings().all()

        if agent_rows:
            agent_lines = ["[Active Agents]"]
            for row in agent_rows:
                name = (row["name"] or row["agent_code"] or "Unknown")[:120]
                desc = (row["description"] or "")[:200]
                line = f"- {name}"
                if desc:
                    line += f": {desc}"
                agent_lines.append(line)
                total_chars += len(line)
                if total_chars > _MAX_CONTEXT_CHARS:
                    break
            parts.append("\n".join(agent_lines))

    return "\n\n".join(parts)


# ── Idempotency key ──────────────────────────────────────────────────────────────


def _make_rewrite_idempotency_key(query: str, project_id: UUID | None) -> str:
    """Generate a stable idempotency key for the rewrite call."""
    raw = f"rewrite:{query}:{project_id or 'all'}"
    return hashlib.sha256(raw.encode()).hexdigest()[:64]


# ── Main rewrite function ────────────────────────────────────────────────────────


def rewrite_query(
    db: Session,
    query: str,
    project_id: UUID | None,
    context: RequestContext,
    *,
    gateway: Gateway | None = None,
) -> RewriteResult:
    """Rewrite a user's search query to resolve vague references.

    Uses LLM (via Gateway) + contextual data from the database to transform
    queries like "上次那个bug" into specific, searchable terms.

    Parameters
    ----------
    db : Session
        Active database session for context gathering.
    query : str
        The user's original search query.
    project_id : UUID or None
        Project context for gathering relevant data.
    context : RequestContext
        The API request context for audit trail.
    gateway : Gateway or None
        Pre-configured Gateway instance (created if None).

    Returns
    -------
    RewriteResult
        Contains the rewritten query (or original as fallback), confidence,
        and explanation.
    """
    result = RewriteResult(original_query=query, rewritten_query=query)

    # Step 1: Gather context
    try:
        context_text = gather_context_for_rewrite(db, project_id=project_id)
    except Exception as exc:
        logger.warning("Failed to gather context for rewrite: %s", exc)
        context_text = ""

    result.context_summary = (
        f"context_chars={len(context_text)}, project_id={project_id}"
    )

    # If no context available, skip rewriting — nothing to resolve against
    if not context_text.strip():
        logger.debug("No context available for query rewriting, skipping")
        return result

    # Step 2: Build prompt
    messages = build_rewrite_prompt(
        query=query,
        context_text=context_text,
    )

    # Step 3: Call LLM via Gateway
    gw = gateway or Gateway()
    try:
        gw_result = gw.call(
            capability_code="chat.completion",
            params={
                "messages": messages,
                "temperature": 0.2,
                "max_tokens": 256,
            },
            project_id=project_id,
            sensitivity="normal",
            actor_type=context.actor.actor_type,
            actor_id=context.actor.actor_id,
            idempotency_key=_make_rewrite_idempotency_key(query, project_id),
            call_type="query_rewrite",
        )
    except GatewayError as exc:
        logger.warning("Gateway call failed for query rewrite: %s", exc)
        return result
    except Exception as exc:
        logger.warning("Unexpected error during query rewrite: %s", exc)
        return result

    raw_response = gw_result.get("data", {}).get("choices", [{}])[0].get(
        "message", {}
    ).get("content", "")

    if not raw_response:
        raw_response = str(gw_result.get("data", ""))

    # Step 4: Parse response
    parsed = parse_rewrite_response(raw_response, original_query=query)
    return parsed


def quick_rewrite(
    db: Session,
    query: str,
    project_id: UUID | None,
    context: RequestContext,
) -> RewriteResult:
    """Convenience wrapper around :func:`rewrite_query` with default Gateway.

    Use this from API endpoints for simpler call sites.
    """
    return rewrite_query(db, query, project_id, context)
