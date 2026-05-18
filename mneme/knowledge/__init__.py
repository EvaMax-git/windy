"""Knowledge engine — chunking, token estimation, FTS search, citation (P3-06/P3-07/P3-08)."""

from mneme.knowledge.chunking import (
    ChunkingStrategy,
    chunk_document,
    chunk_text,
)
from mneme.knowledge.citation import (
    Citation,
    CitationListResult,
    CitationNode,
    build_citation,
    check_stale_documents,
    is_document_stale,
    list_citations,
    list_source_maps,
)
from mneme.knowledge.fts import (
    ensure_fts_index,
    get_index_state,
    init_index_state,
    refresh_stale_fts_indexes,
    search_fts,
)
from mneme.knowledge.token_estimator import estimate_tokens, strip_markdown

__all__ = [
    "ChunkingStrategy",
    "Citation",
    "CitationListResult",
    "CitationNode",
    "build_citation",
    "check_stale_documents",
    "chunk_document",
    "chunk_text",
    "ensure_fts_index",
    "estimate_tokens",
    "get_index_state",
    "init_index_state",
    "is_document_stale",
    "list_citations",
    "list_source_maps",
    "refresh_stale_fts_indexes",
    "search_fts",
    "strip_markdown",
]
