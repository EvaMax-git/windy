"""Context window expansion for knowledge chunk search results.

Given a matched chunk, this module expands the context by:
1. **Surrounding paragraphs** — fetches neighboring chunks from the same document
   (previous N and next N chunks in chunk_order).
2. **Related memories** — searches for memory entries whose title or text overlaps
   with key terms from the matched chunk, providing cross-source context.

This enriches search results so users/agents see not just the exact-match sentence,
but the full paragraph(s) and semantically related memories.

Design
------
* **Surrounding paragraphs**: Uses ``knowledge_chunks.chunk_order`` to find adjacent
  chunks within the same document. Configurable window size (default ±1).
* **Related memories**: Performs a lightweight FTS lookup on ``memory_index_entries``
  using extracted key terms from the chunk text.
* **Merge strategy**: Expands each chunk result in-place by appending
  ``surrounding_chunks`` and ``related_memories`` fields.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Sequence
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from mneme.knowledge.token_estimator import estimate_tokens as _estimate_tokens

# ── Constants ─────────────────────────────────────────────────────────────

DEFAULT_SURROUNDING_RADIUS = 1  # ±1 chunks before/after
DEFAULT_MAX_RELATED_MEMORIES = 3
DEFAULT_RELATED_MEMORY_MIN_SCORE = 0.1

# Stop words for key-term extraction (lowercase, common English + Chinese)
_STOP_WORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "can", "shall", "of", "in", "to", "for",
    "with", "on", "at", "by", "from", "as", "into", "through", "during",
    "before", "after", "above", "below", "between", "and", "but", "or",
    "not", "no", "so", "if", "than", "then", "that", "this", "these",
    "those", "it", "its", "he", "she", "they", "we", "you", "我", "的",
    "了", "是", "在", "不", "和", "也", "有", "这", "那", "就", "都", "而",
    "及", "与", "或", "很", "要", "会", "可以", "一个", "没有",
})


# ── Data classes ──────────────────────────────────────────────────────────


@dataclass
class SurroundingChunk:
    """A neighboring chunk in the same document."""
    chunk_id: UUID
    chunk_order: int
    chunk_text: str
    token_count: int
    relative_position: int  # -N before, +N after the matched chunk


@dataclass
class RelatedMemory:
    """A memory entry related to a knowledge chunk."""
    memory_id: UUID
    title: str
    memory_text_preview: str
    canonical_key: str
    relevance_score: float


@dataclass
class ContextWindowResult:
    """Expanded context for a single matched chunk."""

    chunk_id: UUID
    document_id: UUID
    matched_chunk_text: str
    surrounding_chunks: list[SurroundingChunk] = field(default_factory=list)
    related_memories: list[RelatedMemory] = field(default_factory=list)


@dataclass
class ContextWindowConfig:
    """Configuration for context window expansion."""

    surrounding_radius: int = DEFAULT_SURROUNDING_RADIUS
    max_related_memories: int = DEFAULT_MAX_RELATED_MEMORIES
    related_memory_min_score: float = DEFAULT_RELATED_MEMORY_MIN_SCORE
    enable_surrounding: bool = True
    enable_related_memories: bool = True


# ── SQL templates ─────────────────────────────────────────────────────────

_SURROUNDING_CHUNKS_SQL = text("""
    SELECT
      chunk_id,
      chunk_order,
      chunk_text,
      token_count
    FROM knowledge_chunks
    WHERE document_id = :document_id
      AND chunk_order >= :min_order
      AND chunk_order <= :max_order
      AND chunk_id != :center_chunk_id
    ORDER BY chunk_order ASC
""")


# ── Key-term extraction ───────────────────────────────────────────────────


def _extract_key_terms(text: str, max_terms: int = 5) -> list[str]:
    """Extract key terms from text for related-memory search.

    Strategy: Pick the longest non-stop-word tokens (words/phrases)
    that are not just single characters (for CJK) or very short words.
    """
    # Split on whitespace and punctuation
    tokens = re.findall(r"[\w一-鿿]+", text.lower())

    # Filter stop words and short tokens
    meaningful: list[tuple[str, int]] = []
    for token in tokens:
        if token in _STOP_WORDS:
            continue
        if len(token) < 2:
            continue
        meaningful.append((token, len(token)))

    # Sort by length descending, pick top N
    meaningful.sort(key=lambda x: x[1], reverse=True)
    return [t for t, _ in meaningful[:max_terms]]


# ── Public API ────────────────────────────────────────────────────────────


def expand_chunk_context(
    db: Session,
    *,
    document_id: UUID,
    chunk_id: UUID,
    chunk_order: int,
    matched_chunk_text: str,
    project_id: UUID | None = None,
    store_id: UUID | None = None,
    config: ContextWindowConfig | None = None,
) -> ContextWindowResult:
    """Expand a single chunk match with surrounding paragraphs and related memories.

    Args:
        db: Active SQLAlchemy session.
        document_id: The document containing the matched chunk.
        chunk_id: The primary key of the matched chunk.
        chunk_order: The ``chunk_order`` of the matched chunk within the document.
        matched_chunk_text: The full text of the matched chunk.
        project_id: Optional project filter for related memory search.
        store_id: Optional memory store filter for related memory search.
        config: Expansion configuration (uses defaults if None).

    Returns:
        :class:`ContextWindowResult` with surrounding chunks and related memories.
    """
    cfg = config or ContextWindowConfig()
    result = ContextWindowResult(
        chunk_id=chunk_id,
        document_id=document_id,
        matched_chunk_text=matched_chunk_text,
    )

    # 1. Surrounding paragraphs
    if cfg.enable_surrounding and cfg.surrounding_radius > 0:
        result.surrounding_chunks = _fetch_surrounding_chunks(
            db,
            document_id=document_id,
            center_chunk_id=chunk_id,
            center_order=chunk_order,
            radius=cfg.surrounding_radius,
        )

    # 2. Related memories
    if cfg.enable_related_memories and cfg.max_related_memories > 0:
        result.related_memories = _find_related_memories(
            db,
            chunk_text=matched_chunk_text,
            project_id=project_id,
            store_id=store_id,
            max_results=cfg.max_related_memories,
            min_score=cfg.related_memory_min_score,
        )

    return result


def expand_multiple_chunks(
    db: Session,
    *,
    chunk_info: list[dict[str, Any]],
    project_id: UUID | None = None,
    store_id: UUID | None = None,
    config: ContextWindowConfig | None = None,
) -> dict[UUID, ContextWindowResult]:
    """Expand context for multiple chunk matches at once.

    Args:
        db: Active session.
        chunk_info: List of dicts with keys: ``chunk_id``, ``document_id``,
            ``chunk_order``, ``chunk_text`` (e.g., from FTS search results).
        project_id: Optional project filter.
        store_id: Optional memory store filter.
        config: Expansion configuration.

    Returns:
        Dict mapping ``chunk_id`` → :class:`ContextWindowResult`.
    """
    cfg = config or ContextWindowConfig()
    results: dict[UUID, ContextWindowResult] = {}

    for info in chunk_info:
        chunk_id = info.get("chunk_id")
        if chunk_id is None:
            continue
        if isinstance(chunk_id, str):
            chunk_id = UUID(chunk_id)

        doc_id = info.get("document_id")
        if isinstance(doc_id, str):
            doc_id = UUID(doc_id)
        if doc_id is None:
            continue

        order = info.get("chunk_order", 0)
        text = info.get("chunk_text", "")

        results[chunk_id] = expand_chunk_context(
            db,
            document_id=doc_id,
            chunk_id=chunk_id,
            chunk_order=order,
            matched_chunk_text=text,
            project_id=project_id,
            store_id=store_id,
            config=cfg,
        )

    return results


# ── Internal helpers ──────────────────────────────────────────────────────


def _fetch_surrounding_chunks(
    db: Session,
    *,
    document_id: UUID,
    center_chunk_id: UUID,
    center_order: int,
    radius: int,
) -> list[SurroundingChunk]:
    """Fetch neighboring chunks from the same document."""
    min_order = center_order - radius
    max_order = center_order + radius

    rows = db.execute(
        _SURROUNDING_CHUNKS_SQL,
        {
            "document_id": document_id,
            "min_order": min_order,
            "max_order": max_order,
            "center_chunk_id": center_chunk_id,
        },
    ).mappings().all()

    results: list[SurroundingChunk] = []
    for row in rows:
        order = row["chunk_order"]
        rel_pos = order - center_order
        results.append(SurroundingChunk(
            chunk_id=row["chunk_id"],
            chunk_order=order,
            chunk_text=row["chunk_text"],
            token_count=row["token_count"] or 0,
            relative_position=rel_pos,
        ))

    results.sort(key=lambda c: c.chunk_order)
    return results


def _find_related_memories(
    db: Session,
    *,
    chunk_text: str,
    project_id: UUID | None = None,
    store_id: UUID | None = None,
    max_results: int = 3,
    min_score: float = 0.1,
) -> list[RelatedMemory]:
    """Find memory entries related to the chunk via key-term FTS lookup."""
    key_terms = _extract_key_terms(chunk_text)
    if not key_terms:
        return []

    # Build a tsquery from key terms for PostgreSQL FTS
    query_str = " | ".join(key_terms)

    sql = text("""
        SELECT
            m.memory_id,
            m.title,
            m.memory_text,
            m.canonical_key,
            ts_rank(
                mie.fts_vector,
                to_tsquery('simple', :query)
            ) AS relevance_score
        FROM memory_index_entries mie
        JOIN memories m ON m.memory_id = mie.memory_id
        WHERE mie.fts_state IN ('ready', 'stale')
          AND mie.fts_vector @@ to_tsquery('simple', :query)
          AND m.status IN ('active', 'draft')
          AND (CAST(:project_id AS uuid) IS NULL OR mie.project_id = CAST(:project_id AS uuid))
          AND (CAST(:store_id AS uuid) IS NULL OR m.store_id = CAST(:store_id AS uuid))
        ORDER BY relevance_score DESC
        LIMIT :limit
    """)

    try:
        rows = db.execute(
            sql,
            {
                "query": query_str,
                "project_id": project_id,
                "store_id": store_id,
                "limit": max_results,
            },
        ).mappings().all()
    except Exception:
        # FTS query syntax error (e.g., special chars in key terms) — graceful degrade.
        # The failed SQL has aborted the current transaction; roll back to allow
        # subsequent queries in the same session to continue.
        db.rollback()
        return []

    results: list[RelatedMemory] = []
    for row in rows:
        score = float(row["relevance_score"] or 0.0)
        if score < min_score:
            continue
        memory_text = row["memory_text"] or ""
        preview = memory_text[:300]
        if len(memory_text) > 300:
            preview += "…"

        results.append(RelatedMemory(
            memory_id=row["memory_id"],
            title=row["title"] or "",
            memory_text_preview=preview,
            canonical_key=row["canonical_key"] or "",
            relevance_score=score,
        ))

    return results
