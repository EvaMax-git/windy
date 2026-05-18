"""P5-04 Context module interface abstractions.

ABC definition for the context compiler, allowing different compilation
strategies (knowledge-first, memory-first, hybrid) to be plugged in
without changing callers.

Protocols defined
-----------------
* ``IContextCompiler`` — ``compile(db, context, ...) → dict[str, Any]``
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from mneme.api.context import RequestContext


class IContextCompiler(ABC):
    """Abstract interface for context compilation.

    Compiles a context pack from knowledge chunks and memory entries
    based on a search query, respecting sensitivity ceilings and token
    budgets.

    Implementations may use different retrieval strategies, ranking
    algorithms, and budget-allocation policies.
    """

    @abstractmethod
    def compile(
        self,
        db: Session,
        context: RequestContext,
        *,
        agent_id: UUID | None = None,
        project_id: UUID | None = None,
        query_text: str,
        compile_mode: str = "full",
        token_budget: dict[str, Any] | None = None,
        sensitivity_ceiling: str = "private",
    ) -> dict[str, Any]:
        """Compile a context pack for the given query.

        Returns
        -------
        dict
            A dict with keys: pack, items, total_token_count,
            included_count, excluded_count, degradation_reason.
        """
        ...
