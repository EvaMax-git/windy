"""P5-04 Memory module interface abstractions.

ABC (Abstract Base Class) definitions for the three core memory services.
These allow swapping implementations (e.g. different embedding providers,
search engines, or index strategies) without changing callers.

Protocols defined
-----------------
* ``ISearchEngine``    — ``search(query, ...) → MemorySearchOutput``
* ``IIndexManager``    — lifecycle hooks: ``on_memory_activated``, etc.
* ``IExtractPipeline`` — ``extract_from_source(db, context, ...) → ExtractOutput``
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from mneme.api.context import RequestContext


class ISearchEngine(ABC):
    """Abstract interface for memory search operations.

    Implementations may use FTS, vector, or hybrid search strategies.
    """

    @abstractmethod
    def search(
        self,
        db: Session,
        *,
        query: str,
        project_id: UUID | None = None,
        page: int = 1,
        page_size: int = 20,
        mode: str = "hybrid",
        context: RequestContext | None = None,
    ) -> Any:
        """Search memory entries.

        Returns
        -------
        MemorySearchOutput or compatible object.
        """
        ...


class IIndexManager(ABC):
    """Abstract interface for memory index lifecycle management.

    Provides hooks called after memory lifecycle events (activate, update,
    expire, restore, delete).
    """

    @abstractmethod
    def on_memory_activated(
        self,
        db: Session,
        *,
        memory_id: UUID,
        version: int,
        project_id: UUID,
        title: str | None,
        memory_text: str,
    ) -> None:
        """Create initial index entry when a memory is activated."""
        ...

    @abstractmethod
    def on_memory_updated(
        self,
        db: Session,
        *,
        memory_id: UUID,
        old_version: int,
        new_version: int,
        project_id: UUID,
        title: str | None,
        memory_text: str,
    ) -> None:
        """Mark old entries stale and create a new index entry."""
        ...

    @abstractmethod
    def on_memory_expired(
        self,
        db: Session,
        *,
        memory_id: UUID,
    ) -> None:
        """Mark all index entries as stale when memory expires."""
        ...

    @abstractmethod
    def on_memory_restored(
        self,
        db: Session,
        *,
        memory_id: UUID,
        version: int,
        project_id: UUID,
        title: str | None,
        memory_text: str,
    ) -> None:
        """Create a fresh index entry when memory is restored."""
        ...

    @abstractmethod
    def on_memory_deleted(
        self,
        db: Session,
        *,
        memory_id: UUID,
    ) -> None:
        """Mark all index entries as stale when memory is deleted."""
        ...


class IExtractPipeline(ABC):
    """Abstract interface for the memory extraction pipeline.

    Extracts memory candidates from conversation messages or raw events
    via LLM calls.
    """

    @abstractmethod
    def extract_from_source(
        self,
        db: Session,
        context: RequestContext,
        *,
        source_type: str,
        source_id: UUID,
        project_id: UUID | None = None,
        conversation_context: str = "",
    ) -> Any:
        """Run extraction pipeline for a single source.

        Returns
        -------
        ExtractOutput or compatible object.
        """
        ...
