"""Moderate Strategy — summarized content, medium priority.

Moderate cards (user_profile) inject a *summary* (first ~300 chars
of each memory) by default, but can be expanded to full content
via the ``expand_cards`` override.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from mneme.context.strategies.base import IInjectionStrategy, StrategyResult
from mneme.knowledge.token_estimator import estimate_tokens

_LIST_MEMORIES = text("""
    SELECT memory_id, store_id, canonical_key, title, memory_text,
           sensitivity_level, status, current_version,
           search_weight, quality_score
    FROM memories
    WHERE store_id = :store_id
      AND status = 'active'
    ORDER BY search_weight DESC, created_at DESC
    LIMIT :limit
""")


class ModerateStrategy(IInjectionStrategy):
    """Moderate injection — summarized content, medium budget priority.

    Used for cards that provide useful context but aren't essential
    (e.g. user_profile). Loads up to 10 memories and truncates each
    to ~300 chars for a compact summary.
    """

    name = "moderate"
    priority = 1          # injected second
    budget_ratio = 0.30   # 30% of usable budget

    def fetch_memories(
        self,
        db: Session,
        store_id: UUID,
        query_text: str,
    ) -> list[dict[str, Any]]:
        """Load top-10 active memories by weight."""
        try:
            rows = db.execute(
                _LIST_MEMORIES,
                {"store_id": store_id, "limit": 10},
            ).all()
            return [dict(row._mapping) for row in rows]
        except Exception:
            return []

    def build_content(
        self,
        memories: list[dict[str, Any]],
        budget: int,
    ) -> StrategyResult:
        """Build summarized blocks: truncate each memory to ~300 chars."""
        parts: list[str] = []
        token_count = 0
        mem_ids: list[UUID] = []
        truncated = False

        for mem in memories:
            mem_id = mem.get("memory_id")
            title = mem.get("title") or ""
            text = mem.get("memory_text") or ""

            # Truncate long text to summary (~300 chars)
            if len(text) > 300:
                text = text[:300] + "..."

            entry = f"## {title}\n{text}" if title else text
            entry_tokens = estimate_tokens(entry)

            if token_count + entry_tokens > budget:
                truncated = True
                break

            parts.append(entry)
            token_count += entry_tokens
            mem_ids.append(mem_id)

        if truncated:
            parts.append("[内容已截断 - 超出token预算]")

        return StrategyResult(
            content="\n\n".join(parts) if parts else "[无内容]",
            token_count=token_count,
            memory_ids=mem_ids,
            truncated=truncated,
        )
