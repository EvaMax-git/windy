"""P8-01 Context Assembly Engine schemas.

Defines the API surface for the context assembly engine that assembles
agent context from card-based memory stores with tiered injection strategies.
"""

from __future__ import annotations

from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import Field

from mneme.schemas.common import ApiSchema


class InjectionStrategy(str, Enum):
    """How aggressively a card type's content is injected into context."""
    always = "always"        # 始终注入 — full content, always included
    moderate = "moderate"    # 适量注入 — limited summary, expand on request
    on_demand = "on_demand"  # 按需展开 — only include query-relevant portions


# ── Injection Strategy Class Hierarchy ──────────────────────────────────


class InjectionStrategyBase:
    """Base class for card injection strategy behavior.

    Each subclass defines *how* card content is injected into the assembled
    context — whether it is always included in full, moderately summarized,
    or selectively included on demand.

    This class hierarchy complements the :class:`InjectionStrategy` enum by
    providing executable behavior rather than static labels.
    """

    name: str

    def apply(self, content: str, max_tokens: int | None = None) -> str:
        """Apply the injection strategy to *content*.

        Args:
            content: Raw card content to inject.
            max_tokens: Optional upper bound on returned content size.

        Returns:
            The processed (potentially truncated / summarized) content.
        """
        raise NotImplementedError

    def is_expandable(self) -> bool:
        """Return True if this strategy allows expanding content on demand."""
        raise NotImplementedError


class AlwaysStrategy(InjectionStrategyBase):
    """始终注入 — full content is always included, never truncated.

    This is the most aggressive strategy and consumes the most tokens.
    It is appropriate for foundational cards (e.g. soul_card, identity_card)
    that must *always* be available to the agent.
    """

    name = "always"

    def apply(self, content: str, max_tokens: int | None = None) -> str:
        """Return *content* unchanged — ``always`` cards are never truncated."""
        return content

    def is_expandable(self) -> bool:
        """Always-strategy cards are already fully included; no expansion needed."""
        return False


class ModerateStrategy(InjectionStrategyBase):
    """适量注入 — inject a limited summary; expand to full content on request.

    Moderate cards contribute a token-conscious summary by default but can be
    expanded when the request explicitly asks for detail (e.g. via
    ``expand_cards`` in the assemble request).
    """

    name = "moderate"

    def apply(self, content: str, max_tokens: int | None = None) -> str:
        """Return *content*, truncating if it exceeds *max_tokens*."""
        if max_tokens is not None and len(content) > max_tokens:
            return content[:max_tokens]
        return content

    def is_expandable(self) -> bool:
        """Moderate-strategy cards *can* be expanded to full content."""
        return True


class OnDemandStrategy(InjectionStrategyBase):
    """按需展开 — only include query-relevant portions of the content.

    On-demand cards are the most conservative strategy. The assembly engine
    selects only the slices of content that are semantically relevant to the
    current query. The full content is available when explicitly expanded.
    """

    name = "on_demand"

    def apply(self, content: str, max_tokens: int | None = None) -> str:
        """Return *content* as-is — relevance filtering is done upstream."""
        return content

    def is_expandable(self) -> bool:
        """On-demand cards *can* be expanded to full content."""
        return True


# ── Strategy registry (class → enum mapping) ────────────────────────────
STRATEGY_CLASS_MAP: dict[str, type[InjectionStrategyBase]] = {
    "always": AlwaysStrategy,
    "moderate": ModerateStrategy,
    "on_demand": OnDemandStrategy,
}


# ── Card-type → default strategy mapping ───────────────────────────────
CARD_STRATEGY_MAP: dict[str, InjectionStrategy] = {
    "soul_card":      InjectionStrategy.always,       # 灵魂卡
    "identity_card":  InjectionStrategy.always,       # 身份卡
    "tool_catalog":   InjectionStrategy.always,       # 工具目录
    "user_profile":   InjectionStrategy.moderate,     # 用户画像
    "tool_detail":    InjectionStrategy.on_demand,    # 工具详情
}


class AssembleRequest(ApiSchema):
    """Request to assemble context for an agent query."""
    agent_id: UUID = Field(description="Agent requesting context assembly")
    query_text: str = Field(
        min_length=1,
        max_length=10000,
        description="The user query / task description for relevance matching",
    )
    project_id: UUID | None = Field(
        default=None, description="Optional project scope filter"
    )
    conversation_history: str | None = Field(
        default=None,
        max_length=50000,
        description="Optional conversation history for context awareness",
    )
    max_tokens: int | None = Field(
        default=None,
        ge=512,
        le=1_000_000,
        description="Override max context tokens (default from config)",
    )
    strategy_overrides: dict[str, InjectionStrategy] | None = Field(
        default=None,
        description="Override injection strategy for specific card types. "
                    "Key = card type (soul_card/identity_card/...), "
                    "Value = strategy (always/moderate/on_demand)",
    )
    expand_cards: list[str] | None = Field(
        default=None,
        description="Card types to force-expand regardless of strategy. "
                    "Used when 'moderate' or 'on_demand' cards need full content.",
    )


class CardSection(ApiSchema):
    """A single card section in the assembled context."""
    card_type: str = Field(description="Card type (soul_card/identity_card/etc.)")
    store_id: UUID | None = Field(default=None, description="Source memory_store ID")
    store_name: str | None = Field(default=None, description="Source memory_store name")
    strategy: InjectionStrategy = Field(description="Applied injection strategy")
    content: str = Field(description="Assembled text content for this card")
    token_count: int = Field(ge=0, description="Estimated tokens in this section")
    memory_ids: list[UUID] = Field(
        default_factory=list, description="Referenced memory IDs"
    )
    truncated: bool = Field(
        default=False, description="True if content was truncated due to budget"
    )


class BudgetBreakdown(ApiSchema):
    """Token budget allocation details."""
    total_available: int = Field(description="Total tokens available for context")
    system_overhead: int = Field(description="Tokens reserved for system prompt")
    output_reserve: int = Field(description="Tokens reserved for model output")
    usable: int = Field(description="Tokens usable for cards after reserves")
    always_used: int = Field(ge=0, description="Tokens consumed by 'always' cards")
    moderate_used: int = Field(ge=0, description="Tokens consumed by 'moderate' cards")
    on_demand_used: int = Field(ge=0, description="Tokens consumed by 'on_demand' cards")
    remaining: int = Field(ge=0, description="Unused tokens remaining")


class AssembleResponse(ApiSchema):
    """Assembled context response."""
    agent_id: UUID
    assembled_text: str = Field(
        description="Final assembled context text, ready for injection into LLM prompt"
    )
    sections: list[CardSection] = Field(
        description="Individual card sections in assembly order"
    )
    budget: BudgetBreakdown = Field(description="Token budget breakdown")
    total_tokens: int = Field(ge=0, description="Total estimated tokens")
    strategy_summary: dict[str, str] = Field(
        description="Card type → applied strategy mapping"
    )
    degradation_reason: str | None = Field(
        default=None, description="Non-null if assembly was degraded"
    )
