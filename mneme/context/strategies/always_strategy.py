"""Always Strategy — full content, top priority.

Always cards (soul_card, identity_card, tool_catalog) are injected
in their entirety. All active memories are loaded, and content is
capped only by the per-tier token budget.
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


class AlwaysStrategy(IInjectionStrategy):
    """Always inject — full content, highest budget priority.

    Used for cards that *must* be present in every context (soul,
    identity, tool catalog). Loads up to 50 memories per store,
    includes full text, and claims the largest budget share.
    """

    name = "always"
    priority = 0          # injected first
    budget_ratio = 0.50   # 50% of usable budget

    def fetch_memories(
        self,
        db: Session,
        store_id: UUID,
        query_text: str,
    ) -> list[dict[str, Any]]:
        """Load all active memories (up to 50), ordered by weight."""
        try:
            rows = db.execute(
                _LIST_MEMORIES,
                {"store_id": store_id, "limit": 50},
            ).all()
            return [dict(row._mapping) for row in rows]
        except Exception:
            return []

    def build_content(
        self,
        memories: list[dict[str, Any]],
        budget: int,
    ) -> StrategyResult:
        """Build full-content blocks until the budget is exhausted."""
        parts: list[str] = []
        token_count = 0
        mem_ids: list[UUID] = []
        truncated = False

        for mem in memories:
            mem_id = mem.get("memory_id")
            title = mem.get("title") or ""
            text = mem.get("memory_text") or ""

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
