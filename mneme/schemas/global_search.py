"""Global search schemas — aggregated cross-domain search (agents + knowledge + memory).

Schema alignment
----------------
All enumerations match the DDL CHECK constraints and existing schemas.
"""

from __future__ import annotations

from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import Field

from mneme.schemas.common import ApiSchema, PaginatedData


class GlobalSearchSource(str, Enum):
    """Source domain of a global search result."""

    agent = "agent"
    knowledge = "knowledge"
    memory = "memory"
    ppr_graph = "ppr_graph"           # PPR graph traversal recall
    temporal_cluster = "temporal_cluster"  # Temporal shape clustering


class GlobalSearchResult(ApiSchema):
    """A single global search result item."""

    source: GlobalSearchSource
    result_id: str = Field(description="UUID of the matched row (agent_id, chunk_id, or memory_id)")
    title: str = Field(description="Display title — agent name, document title, or memory title")
    snippet: str = Field(description="Truncated matching text with context (≤300 chars)")
    rank: float = Field(default=0.0, description="Similarity / relevance score (higher = better)")
    icon: str = Field(
        default="",
        description="Icon hint for rendering: 'bot', 'book-open', 'brain'",
    )
    link: str = Field(default="", description="Frontend route to navigate to on selection")
    meta: dict[str, Any] = Field(
        default_factory=dict,
        description="Extra context (e.g. sensitivity_level, status, project_id)",
    )


class QueryRewriteInfo(ApiSchema):
    """Metadata about query rewriting applied to the search query."""

    original_query: str = Field(description="The user's original query before rewriting")
    rewritten_query: str = Field(description="The query after rewriting (same as original if no rewrite)")
    is_rewritten: bool = Field(default=False, description="Whether the query was actually modified")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0, description="LLM confidence in the rewrite")
    explanation: str = Field(default="", description="Human-readable explanation of what was resolved")


class GlobalSearchResponse(ApiSchema):
    """Paginated global search response."""

    items: list[GlobalSearchResult]
    total: int = Field(default=0)
    query_time_ms: float = Field(default=0.0)
    source_counts: dict[str, int] = Field(
        default_factory=dict,
        description="Breakdown: {'agent': N, 'knowledge': N, 'memory': N, 'ppr_graph': N, 'temporal_cluster': N}",
    )
    rewrite_info: QueryRewriteInfo | None = Field(
        default=None,
        description="Query rewriting metadata (present only when rewrite was attempted)",
    )
    rerank_applied: bool = Field(
        default=False,
        description="Whether cross-encoder re-ranking was applied to the result set (P5-03)",
    )
