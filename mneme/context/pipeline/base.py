"""Pipeline Step ABC and PipelineContext — the orchestration primitives.

The context assembly engine is decomposed into a sequence of
``PipelineStep`` implementations.  Each step receives a
``PipelineContext`` dataclass (the shared state bag) and returns an
updated context.  Steps are idempotent: running the same step twice
with the same input produces the same output.

Rollback is supported for steps that create side-effects (e.g. DB
writes).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from mneme.api.context import RequestContext


@dataclass
class PipelineContext:
    """Mutable state bag carried through the assembly pipeline.

    Attributes set by the *caller* (input):
    ---------------------------------------
    db : Session
        Active SQLAlchemy session.
    request_ctx : RequestContext
        Request context (actor, idempotency key, etc.).
    agent_id : UUID
        The agent requesting context.
    query_text : str
        The user query for relevance matching.
    project_id : UUID | None
        Optional project scope.
    conversation_history : str | None
        Optional conversation history to prepend.
    max_tokens : int | None
        Override for max context tokens.
    strategy_overrides : dict | None
        Per-request card → strategy overrides.
    expand_cards : list[str] | None
        Card types to force-expand.

    Attributes set by *steps* (output):
    -----------------------------------
    stores_by_type : dict[str, list[dict]]
        Card stores grouped by card type.
    strategies : dict[str, str]
        Card type → resolved strategy name.
    budget : dict
        Token budget breakdown.
    sections : list[dict]
        Assembled card sections.
    assembled_text : str
        Final assembled context text.
    total_tokens : int
        Estimated total tokens.
    strategy_summary : dict[str, str]
        Card type → applied strategy name.
    degradation_reason : str | None
        Non-null if assembly was degraded.
    context_pack : dict | None
        Written context_pack record.
    settings : Any
        Application settings (lazy-loaded).
    """

    # ── Input ────────────────────────────────────────────────────────────
    db: Session
    request_ctx: RequestContext
    agent_id: UUID
    query_text: str
    project_id: UUID | None = None
    conversation_history: str | None = None
    max_tokens: int | None = None
    strategy_overrides: dict[str, str] | None = None
    expand_cards: list[str] | None = None

    # ── Output (populated by steps) ──────────────────────────────────────
    stores_by_type: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    strategies: dict[str, str] = field(default_factory=dict)
    budget: dict[str, Any] = field(default_factory=dict)
    sections: list[dict[str, Any]] = field(default_factory=list)
    assembled_text: str = ""
    total_tokens: int = 0
    strategy_summary: dict[str, str] = field(default_factory=dict)
    degradation_reason: str | None = None
    context_pack: dict[str, Any] | None = None
    settings: Any = None


class PipelineStep(ABC):
    """A discrete, composable step in the context assembly pipeline.

    Each step:
    1. Reads from ``PipelineContext``
    2. Performs its work
    3. Writes results back into ``PipelineContext``
    4. Returns the (possibly mutated) context
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable step name for logging / debugging."""
        ...

    @abstractmethod
    def execute(self, ctx: PipelineContext) -> PipelineContext:
        """Execute this step and return the updated context.

        Must be idempotent: re-running with the same ``ctx`` produces
        the same result.
        """
        ...

    def rollback(self, ctx: PipelineContext) -> PipelineContext:
        """Reverse side-effects.  Default is no-op."""
        return ctx
