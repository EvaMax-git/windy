"""P8-01 Context Assembly Engine.

Orchestrates context assembly for agent queries following the card-based
injection strategy model:

1. Resolve agent → memory_stores (cards) grouped by card type
2. Load card content from bound memory_stores
3. Apply injection strategy per card type (always / moderate / on_demand)
4. Enforce token budget with configurable allocation ratios
5. Assemble final context text with section headers
6. Write audit event + context_pack for traceability

**Architecture**: Strategy Pattern + Pipeline Orchestration.

- **Strategies** (``mneme.context.strategies``) encapsulate *how* card
  content is loaded and formatted.  New strategies can be registered
  without modifying the engine core.

- **Pipeline** (``mneme.context.pipeline``) decomposes the assembly
  into discrete, composable steps.  Steps can be reordered, replaced,
  or extended without touching the engine's public API.

The ``assemble_context`` function remains the stable public entry-point
with the same signature as before the refactor.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from mneme.api.context import RequestContext
from mneme.context.pipeline.base import PipelineContext
from mneme.context.pipeline.orchestrator import PipelineOrchestrator

# Re-export for backward compatibility (used by tests)
from mneme.context.strategies.registry import DEFAULT_CARD_STRATEGY_MAP as _DEFAULT_MAP
from mneme.schemas.context_assembly import CARD_STRATEGY_MAP, InjectionStrategy

logger = logging.getLogger(__name__)


# ── Backward-compatible helpers ────────────────────────────────────────────

def _content_hash(text: str) -> str:
    """SHA-256 content digest helper (backward-compatible export)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _strategy_for(card_type: str) -> InjectionStrategy:
    """Map a card type to its default ``InjectionStrategy`` enum value.

    Deprecated in favour of ``get_card_strategy_map()`` from the
    strategy registry.  Kept for backward compatibility with tests.
    """
    from mneme.context.strategies.registry import DEFAULT_CARD_STRATEGY_MAP
    name = DEFAULT_CARD_STRATEGY_MAP.get(card_type, "moderate")
    try:
        return InjectionStrategy(name)
    except ValueError:
        return InjectionStrategy.moderate


# ── Public API ─────────────────────────────────────────────────────────────

def assemble_context(
    db: Session,
    context: RequestContext,
    *,
    agent_id: UUID,
    query_text: str,
    project_id: UUID | None = None,
    conversation_history: str | None = None,
    max_tokens: int | None = None,
    strategy_overrides: dict[str, str] | None = None,
    expand_cards: list[str] | None = None,
) -> dict[str, Any]:
    """Assemble context for an agent query using card-based injection strategies.

    This is the stable public entry-point.  Internally it delegates to
    the ``PipelineOrchestrator``, which runs the following steps:

    1. **ResolveStoresStep** — query agent's card-type memory_stores
    2. **ResolveStrategiesStep** — map card types → injection strategies
    3. **AllocateBudgetStep** — partition token budget across tiers
    4. **LoadContentStep** — use strategies to fetch & format content
    5. **AssembleTextStep** — combine sections + history into final text
    6. **WriteAuditStep** — persist context_pack + audit + outbox

    Parameters
    ----------
    db : Session
        Active SQLAlchemy session.
    context : RequestContext
        Request context with actor info.
    agent_id : UUID
        The agent requesting context assembly.
    query_text : str
        The user query for relevance matching (on_demand cards).
    project_id : UUID | None
        Optional project scope.
    conversation_history : str | None
        Optional conversation history to prepend.
    max_tokens : int | None
        Override max context tokens.
    strategy_overrides : dict | None
        Override injection strategy for specific card types
        (e.g. ``{"soul_card": "moderate"}``).
    expand_cards : list[str] | None
        Card types to force-expand to "always".

    Returns
    -------
    dict
        Keys: assembled_text, sections, budget, total_tokens,
        strategy_summary, degradation_reason.
    """
    # Build pipeline context
    ctx = PipelineContext(
        db=db,
        request_ctx=context,
        agent_id=agent_id,
        query_text=query_text,
        project_id=project_id,
        conversation_history=conversation_history,
        max_tokens=max_tokens,
        strategy_overrides=strategy_overrides,
        expand_cards=expand_cards,
    )

    # Run the default pipeline
    orchestrator = PipelineOrchestrator()
    ctx = orchestrator.run(ctx)

    # Compute remaining budget (backward-compatible format)
    consumed_total = sum(ctx.budget.get("consumed", {}).values())
    usable = ctx.budget.get("usable", 0)

    return {
        "assembled_text": ctx.assembled_text,
        "sections": ctx.sections,
        "budget": {
            "total_available": ctx.budget.get("total_available", 0),
            "system_overhead": ctx.budget.get("system_overhead", 0),
            "output_reserve": ctx.budget.get("output_reserve", 0),
            "usable": usable,
            "always_used": ctx.budget.get("consumed", {}).get("always", 0),
            "moderate_used": ctx.budget.get("consumed", {}).get("moderate", 0),
            "on_demand_used": ctx.budget.get("consumed", {}).get("on_demand", 0),
            "remaining": max(0, usable - consumed_total),
        },
        "total_tokens": ctx.total_tokens,
        "strategy_summary": ctx.strategy_summary,
        "degradation_reason": ctx.degradation_reason,
    }
