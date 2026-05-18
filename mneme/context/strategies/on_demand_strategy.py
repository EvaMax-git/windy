"""On-Demand Strategy — query-relevant snippets, lowest priority.

On-demand cards (tool_detail) are only injected when the query
is relevant. Uses PostgreSQL full-text search to find matching
memories, with a fallback to top-N by weight if FTS fails.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from mneme.context.strategies.base import IInjectionStrategy, StrategyResult
from mneme.knowledge.token_estimator import estimate_tokens

_FTS_SEARCH = text("""
    SELECT
        m.memory_id, m.store_id, m.canonical_key, m.title, m.memory_text,
        m.sensitivity_level, m.status, m.current_version,
        m.search_weight, m.quality_score,
        ts_rank(mie.index_tsvector, plainto_tsquery('simple', :query)) AS rank
    FROM memories m
    JOIN memory_index_entries mie ON mie.memory_id = m.memory_id
    WHERE m.store_id = :store_id
      AND m.status = 'active'
      AND mie.index_tsvector @@ plainto_tsquery('simple', :query)
    ORDER BY rank DESC
    LIMIT :limit
""")

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


class OnDemandStrategy(IInjectionStrategy):
    """On-demand injection — query-relevant snippets, lowest priority.

    Used for cards that *may* be relevant depending on the query
    (e.g. tool_detail). Performs FTS search against the query;
    falls back to top-10 by weight when FTS returns nothing.
    """

    name = "on_demand"
    priority = 2          # injected last
    budget_ratio = 0.20   # 20% of usable budget

    def fetch_memories(
        self,
        db: Session,
        store_id: UUID,
        query_text: str,
    ) -> list[dict[str, Any]]:
        """FTS search for query-relevant memories; fallback to top-10."""
        if query_text:
            try:
                rows = db.execute(
                    _FTS_SEARCH,
                    {"store_id": store_id, "query": query_text, "limit": 10},
                ).all()
                if rows:
                    return [dict(row._mapping) for row in rows]
            except Exception:
                pass

        # Fallback: top-10 by weight
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
        """Build relevance snippets: cap each memory at 2000 chars."""
        parts: list[str] = []
        token_count = 0
        mem_ids: list[UUID] = []
        truncated = False

        for mem in memories:
            mem_id = mem.get("memory_id")
            title = mem.get("title") or ""
            text = mem.get("memory_text") or ""

            # Cap per-memory length for on-demand snippets
            if len(text) > 2000:
                text = text[:2000]

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
