"""Injection Strategy ABC — the core abstraction for card content injection.

Each injection strategy defines **how** a card type's memory content is
loaded, formatted, and injected into the assembled context. Strategies are
pluggable: new strategies can be registered without modifying the engine core.

Strategy Contract
-----------------
* ``name`` — unique identifier (e.g. ``"always"``, ``"moderate"``)
* ``priority`` — ordering priority (lower = injected earlier)
* ``budget_ratio`` — share of the usable token budget this tier claims
* ``fetch_memories`` — retrieve memories from a store (full list, FTS, etc.)
* ``build_content`` — format/truncate memories into a content string
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session


@dataclass
class StrategyResult:
    """Result of a strategy's content-loading phase for one store.

    Attributes
    ----------
    content : str
        Formatted content text ready for assembly.
    token_count : int
        Estimated token count of the content.
    memory_ids : list[UUID]
        Referenced memory IDs (for audit / traceability).
    truncated : bool
        True if content was truncated due to budget constraints.
    """

    content: str = ""
    token_count: int = 0
    memory_ids: list[UUID] = field(default_factory=list)
    truncated: bool = False


class IInjectionStrategy(ABC):
    """Abstract injection strategy for card-type content assembly.

    A strategy encapsulates the *how* of loading card content:
    - Which memories to fetch (all, top-N, FTS-relevant)
    - How to format them (full text, summary, snippet)
    - How to enforce token budgets
    """

    # ── Metadata ─────────────────────────────────────────────────────────

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique strategy identifier (e.g. ``"always"``)."""
        ...

    @property
    @abstractmethod
    def priority(self) -> int:
        """Ordinal for section ordering. Lower = earlier in assembled text."""
        ...

    @property
    @abstractmethod
    def budget_ratio(self) -> float:
        """Fraction (0.0–1.0) of the usable token budget this strategy claims."""
        ...

    # ── Behaviour ────────────────────────────────────────────────────────

    @abstractmethod
    def fetch_memories(
        self,
        db: Session,
        store_id: UUID,
        query_text: str,
    ) -> list[dict[str, Any]]:
        """Fetch memories from *store_id* appropriate for this strategy.

        Parameters
        ----------
        db : Session
            Active SQLAlchemy session.
        store_id : UUID
            The ``memory_store`` to query.
        query_text : str
            Current user query (used for relevance matching by FTS strategies).

        Returns
        -------
        list[dict]
            Memory rows. Each dict must include at least:
            ``memory_id``, ``title``, ``memory_text``.
        """
        ...

    @abstractmethod
    def build_content(
        self,
        memories: list[dict[str, Any]],
        budget: int,
    ) -> StrategyResult:
        """Format a list of memories into an assembled content block.

        Parameters
        ----------
        memories : list[dict]
            Memory rows from ``fetch_memories``.
        budget : int
            Maximum token budget for this content block.

        Returns
        -------
        StrategyResult
            Formatted content, token count, referenced IDs, truncation flag.
        """
        ...
