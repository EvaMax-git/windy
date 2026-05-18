"""P5-02 hybrid memory search.

The vector side uses Gateway-created embeddings only. When vectors are missing
or query embedding fails, search degrades to the existing text path and reports
that state explicitly.

Cross-encoder re-ranking (P5-03) replaces the previous weighted-hybrid scoring
(0.55 × fts + 0.45 × vector).  When Gateway ``rerank.execute`` is available,
candidates are re-ranked by a cross-encoder model; otherwise the weighted-hybrid
fallback is used.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import bindparam, inspect, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Session

from mneme.api.context import RequestContext
from mneme.memory.embedding import EmbeddingError, embed_text
from mneme.observability.health import (
    get_vector_service_status,
    mark_vector_service_degraded,
    reset_vector_service_state,
)
from mneme.search.reranker import (
    CrossEncoderReranker,
    RerankCandidate,
    RerankerError,
    get_reranker,
)

logger = logging.getLogger(__name__)

SearchMode = Literal["fts", "vector", "hybrid", "rerank"]


@dataclass(frozen=True)
class MemorySearchOutput:
    rows: list[dict[str, Any]]
    total: int
    search_mode: str
    degraded: bool = False
    degradation_reason: str | None = None
    stale_count: int = 0
    rerank_applied: bool = False


_TEXT_CANDIDATES = text("""
    SELECT
      mie.memory_index_entry_id, mie.memory_id, mie.memory_version,
      mie.index_text, mie.fts_state, mie.vector_state,
      CAST(0.1 AS float) AS fts_rank,
      CAST(0.0 AS float) AS vector_rank,
      m.title, m.memory_text, m.sensitivity_level,
      m.canonical_key, m.status, m.current_version
    FROM memory_index_entries mie
    JOIN memories m ON m.memory_id = mie.memory_id
    WHERE mie.fts_state IN ('ready', 'stale')
      AND lower(mie.index_text) LIKE :pattern
      AND (:project_id IS NULL OR mie.project_id = :project_id)
      AND (:store_id IS NULL OR m.store_id = :store_id)
      AND m.status IN ('active', 'draft')
    ORDER BY m.updated_at DESC
    LIMIT :limit
""").bindparams(
    bindparam("project_id", type_=PG_UUID(as_uuid=True)),
    bindparam("store_id", type_=PG_UUID(as_uuid=True)),
)

_PG_FTS_CANDIDATES = text("""
    SELECT
      mie.memory_index_entry_id, mie.memory_id, mie.memory_version,
      mie.index_text, mie.fts_state, mie.vector_state,
      ts_rank(mie.fts_vector, plainto_tsquery('simple', :query)) AS fts_rank,
      CAST(0.0 AS float) AS vector_rank,
      m.title, m.memory_text, m.sensitivity_level,
      m.canonical_key, m.status, m.current_version
    FROM memory_index_entries mie
    JOIN memories m ON m.memory_id = mie.memory_id
    WHERE mie.fts_state IN ('ready', 'stale')
      AND mie.fts_vector @@ plainto_tsquery('simple', :query)
      AND (:project_id IS NULL OR mie.project_id = :project_id)
      AND (:store_id IS NULL OR m.store_id = :store_id)
      AND m.status IN ('active', 'draft')
    ORDER BY fts_rank DESC
    LIMIT :limit
""").bindparams(
    bindparam("project_id", type_=PG_UUID(as_uuid=True)),
    bindparam("store_id", type_=PG_UUID(as_uuid=True)),
)

_READY_VECTOR_COUNT = text("""
    SELECT count(*)
    FROM memory_index_entries mie
    JOIN memories m ON m.memory_id = mie.memory_id
    WHERE mie.vector_state = 'ready'
      AND mie.embedding IS NOT NULL
      AND (:project_id IS NULL OR mie.project_id = :project_id)
      AND (:store_id IS NULL OR m.store_id = :store_id)
      AND m.status IN ('active', 'draft')
""").bindparams(
    bindparam("project_id", type_=PG_UUID(as_uuid=True)),
    bindparam("store_id", type_=PG_UUID(as_uuid=True)),
)

_PG_VECTOR_CANDIDATES = text("""
    SELECT
      mie.memory_index_entry_id, mie.memory_id, mie.memory_version,
      mie.index_text, mie.fts_state, mie.vector_state,
      CAST(0.0 AS float) AS fts_rank,
      (1 - (mie.embedding <=> CAST(:embedding AS vector))) AS vector_rank,
      m.title, m.memory_text, m.sensitivity_level,
      m.canonical_key, m.status, m.current_version
    FROM memory_index_entries mie
    JOIN memories m ON m.memory_id = mie.memory_id
    WHERE mie.vector_state = 'ready'
      AND mie.embedding IS NOT NULL
      AND (:project_id IS NULL OR mie.project_id = :project_id)
      AND (:store_id IS NULL OR m.store_id = :store_id)
      AND m.status IN ('active', 'draft')
    ORDER BY mie.embedding <=> CAST(:embedding AS vector)
    LIMIT :limit
""").bindparams(
    bindparam("project_id", type_=PG_UUID(as_uuid=True)),
    bindparam("store_id", type_=PG_UUID(as_uuid=True)),
)

_GENERIC_VECTOR_CANDIDATES = text("""
    SELECT
      mie.memory_index_entry_id, mie.memory_id, mie.memory_version,
      mie.index_text, mie.fts_state, mie.vector_state, mie.embedding,
      CAST(0.0 AS float) AS fts_rank,
      CAST(0.0 AS float) AS vector_rank,
      m.title, m.memory_text, m.sensitivity_level,
      m.canonical_key, m.status, m.current_version
    FROM memory_index_entries mie
    JOIN memories m ON m.memory_id = mie.memory_id
    WHERE mie.vector_state = 'ready'
      AND mie.embedding IS NOT NULL
      AND (:project_id IS NULL OR mie.project_id = :project_id)
      AND (:store_id IS NULL OR m.store_id = :store_id)
      AND m.status IN ('active', 'draft')
    LIMIT :limit
""").bindparams(
    bindparam("project_id", type_=PG_UUID(as_uuid=True)),
    bindparam("store_id", type_=PG_UUID(as_uuid=True)),
)


def _row_to_dict(row: Any) -> dict[str, Any]:
    return dict(row._mapping)


def _candidate_limit(page: int, page_size: int) -> int:
    return min(max(page * page_size, 100), 1000)


def _embedding_literal(vector: list[float]) -> str:
    return json.dumps([float(v) for v in vector], separators=(",", ":"))


def _has_embedding_column(db: Session) -> bool:
    try:
        columns = inspect(db.get_bind()).get_columns("memory_index_entries")
    except Exception:
        return db.get_bind().dialect.name == "postgresql"
    return any(col.get("name") == "embedding" for col in columns)


def _text_candidates(
    db: Session,
    *,
    query: str,
    project_id: UUID | None,
    store_id: UUID | None,
    limit: int,
) -> list[dict[str, Any]]:
    params = {
        "query": query,
        "pattern": f"%{query.lower()}%",
        "project_id": project_id,
        "store_id": store_id,
        "limit": limit,
    }
    if db.get_bind().dialect.name == "postgresql":
        try:
            rows = db.execute(_PG_FTS_CANDIDATES, params).all()
            if rows:
                return [_row_to_dict(row) for row in rows]
        except Exception:
            pass

    rows = db.execute(_TEXT_CANDIDATES, params).all()
    return [_row_to_dict(row) for row in rows]


def _ready_vectors_available(db: Session, *, project_id: UUID | None, store_id: UUID | None = None) -> bool:
    if not _has_embedding_column(db):
        return False
    try:
        return db.execute(
            _READY_VECTOR_COUNT,
            {"project_id": project_id, "store_id": store_id},
        ).scalar_one() > 0
    except Exception:
        return False


def _parse_stored_embedding(value: Any) -> list[float] | None:
    if value is None:
        return None
    if isinstance(value, list):
        raw = value
    elif isinstance(value, str):
        try:
            raw = json.loads(value)
        except json.JSONDecodeError:
            return None
    else:
        return None
    try:
        return [float(v) for v in raw]
    except (TypeError, ValueError):
        return None


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    size = min(len(left), len(right))
    if size == 0:
        return 0.0
    dot = sum(left[i] * right[i] for i in range(size))
    left_norm = math.sqrt(sum(left[i] * left[i] for i in range(size)))
    right_norm = math.sqrt(sum(right[i] * right[i] for i in range(size)))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def _vector_candidates(
    db: Session,
    *,
    query_embedding: list[float],
    project_id: UUID | None,
    store_id: UUID | None,
    limit: int,
) -> list[dict[str, Any]]:
    params = {
        "embedding": _embedding_literal(query_embedding),
        "project_id": project_id,
        "store_id": store_id,
        "limit": limit,
    }
    if db.get_bind().dialect.name == "postgresql":
        rows = db.execute(_PG_VECTOR_CANDIDATES, params).all()
        return [_row_to_dict(row) for row in rows]

    rows = db.execute(_GENERIC_VECTOR_CANDIDATES, params).all()
    candidates: list[dict[str, Any]] = []
    for row in rows:
        item = _row_to_dict(row)
        stored = _parse_stored_embedding(item.pop("embedding", None))
        if stored is None:
            continue
        item["vector_rank"] = _cosine_similarity(query_embedding, stored)
        candidates.append(item)
    candidates.sort(key=lambda item: item.get("vector_rank", 0.0), reverse=True)
    return candidates[:limit]


def _stale_reason(row: dict[str, Any]) -> str | None:
    reasons: list[str] = []
    if row.get("fts_state") == "stale":
        reasons.append("fts_stale")
    if row.get("vector_state") == "stale":
        reasons.append("vector_stale")
    return ",".join(reasons) if reasons else None


def _merge_candidates(
    *,
    query: str,
    fts_rows: list[dict[str, Any]],
    vector_rows: list[dict[str, Any]],
    actual_mode: str,
    degraded: bool,
    degradation_reason: str | None,
    project_id: UUID | None = None,
    context: RequestContext | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Merge FTS and vector candidates, then re-rank via cross-encoder.

    Cross-encoder re-ranking (via Gateway ``rerank.execute``) replaces the
    previous weighted-hybrid formula.  When the reranker is unavailable or
    fails, the old weighted hybrid is used as a fallback.

    Returns:
        Tuple of ``(merged_rows, rerank_applied)``.
    """
    # --- Step 1: merge FTS + vector rows by memory_index_entry_id ----------
    merged: dict[str, dict[str, Any]] = {}
    for row in fts_rows:
        key = str(row["memory_index_entry_id"])
        merged[key] = {**row, "fts_rank": float(row.get("fts_rank") or 0.0)}
    for row in vector_rows:
        key = str(row["memory_index_entry_id"])
        existing = merged.get(key)
        if existing is None:
            merged[key] = {**row, "vector_rank": float(row.get("vector_rank") or 0.0)}
        else:
            existing["vector_rank"] = float(row.get("vector_rank") or 0.0)
            existing["vector_state"] = row.get("vector_state", existing.get("vector_state"))

    if not merged:
        return [], False

    # --- Step 2: build RerankCandidate objects -----------------------------
    # Build candidate text from index_text (or title + memory_text) for the
    # cross-encoder to evaluate.
    candidates: list[RerankCandidate] = []
    keys_by_idx: list[str] = []
    for row in merged.values():
        # Prefer index_text (pre-computed for search), fall back to title+text
        candidate_text = (row.get("index_text") or "").strip()
        if not candidate_text:
            title = row.get("title") or ""
            mem_text = row.get("memory_text") or ""
            candidate_text = f"{title}\n{mem_text}".strip()
        if not candidate_text:
            candidate_text = row.get("canonical_key") or ""

        key = str(row["memory_index_entry_id"])
        keys_by_idx.append(key)
        candidates.append(RerankCandidate(
            id=key,
            text=candidate_text,
            fts_score=float(row.get("fts_rank") or 0.0),
            vector_score=float(row.get("vector_rank") or 0.0),
            meta={
                "memory_id": str(row.get("memory_id", "")),
                "memory_version": row.get("memory_version"),
                "title": row.get("title"),
            },
        ))

    # --- Step 3: cross-encoder reranking -----------------------------------
    reranker = get_reranker()
    reranker.fts_weight = 0.55
    reranker.vector_weight = 0.45

    rerank_applied = False
    try:
        if actual_mode in ("hybrid", "rerank") and len(candidates) > 1:
            reranked = reranker.rerank(
                query=query,
                candidates=candidates,
                project_id=project_id,
                context=context,
            )
            rerank_applied = True
        elif actual_mode == "vector":
            reranked = reranker._weighted_hybrid_fallback(
                candidates, top_n=len(candidates),
            )
        else:
            reranked = reranker._weighted_hybrid_fallback(
                candidates, top_n=len(candidates),
            )
    except RerankerError:
        logger.warning("Reranker failed, using weighted-hybrid fallback")
        reranked = reranker._weighted_hybrid_fallback(
            candidates, top_n=len(candidates),
        )
        rerank_applied = False

    # Build score lookup
    score_map: dict[str, float] = {r.id: r.score for r in reranked}

    # --- Step 4: assemble output rows with final rank ----------------------
    output: list[dict[str, Any]] = []
    for row in merged.values():
        key = str(row["memory_index_entry_id"])
        fts_rank = float(row.get("fts_rank") or 0.0)
        vector_rank = float(row.get("vector_rank") or 0.0)

        # Use reranker score if available, otherwise compute hybrid fallback
        rank = score_map.get(key)
        if rank is None:
            if actual_mode == "hybrid":
                rank = (0.55 * fts_rank) + (0.45 * vector_rank)
            elif actual_mode == "vector":
                rank = vector_rank
            else:
                rank = fts_rank

        stale_reason = _stale_reason(row)
        output.append(
            {
                **row,
                "rank": rank,
                "fts_rank": fts_rank,
                "vector_rank": vector_rank,
                "search_mode": actual_mode,
                "degraded": degraded,
                "degradation_reason": degradation_reason,
                "stale": stale_reason is not None,
                "stale_reason": stale_reason,
            }
        )

    output.sort(key=lambda item: item.get("rank", 0.0), reverse=True)
    return output, rerank_applied


def search_memories(
    db: Session,
    *,
    query: str,
    project_id: UUID | None = None,
    store_id: UUID | None = None,
    page: int = 1,
    page_size: int = 20,
    mode: SearchMode = "hybrid",
    context: RequestContext | None = None,
) -> MemorySearchOutput:
    """Search memory entries with hybrid vector/FTS ranking and degradation.

    When ``store_id`` is provided, results are restricted to memories belonging
    to that memory store only — enabling per-agent memory isolation.
    """
    limit = _candidate_limit(page, page_size)
    requested_mode: SearchMode = mode
    actual_mode = mode
    degraded = False
    degradation_reason: str | None = None

    fts_rows: list[dict[str, Any]] = []
    vector_rows: list[dict[str, Any]] = []

    if requested_mode in ("fts", "hybrid"):
        fts_rows = _text_candidates(
            db, query=query, project_id=project_id, store_id=store_id, limit=limit,
        )

    if requested_mode in ("vector", "hybrid"):
        if not _ready_vectors_available(db, project_id=project_id, store_id=store_id):
            degraded = True
            degradation_reason = "vector_unavailable"
            actual_mode = "fts"
            if not fts_rows:
                fts_rows = _text_candidates(
                    db, query=query, project_id=project_id, store_id=store_id, limit=limit,
                )
        else:
            # Check cached vector service health before attempting Gateway call
            vs_state, vs_reason = get_vector_service_status()
            if vs_state in ("degraded", "unavailable"):
                degraded = True
                degradation_reason = f"vector_service_{vs_state}" + (f":{vs_reason}" if vs_reason else "")
                actual_mode = "fts"
                if not fts_rows:
                    fts_rows = _text_candidates(
                        db, query=query, project_id=project_id, store_id=store_id, limit=limit,
                    )
            else:
                try:
                    query_embedding = embed_text(
                        query,
                        project_id=project_id,
                        context=context,
                        idempotency_key=(
                            f"memory-search-vector-{context.request_id}"
                            if context and not context.idempotency_key
                            else None
                        ),
                    ).embedding
                    vector_rows = _vector_candidates(
                        db,
                        query_embedding=query_embedding,
                        project_id=project_id,
                        store_id=store_id,
                        limit=limit,
                    )
                    if requested_mode == "vector":
                        actual_mode = "vector"
                    else:
                        actual_mode = "hybrid"
                    # Vector service is working — reset any cached failure state
                    if vs_state != "ok":
                        reset_vector_service_state()
                except EmbeddingError as exc:
                    degraded = True
                    degradation_reason = f"query_embedding_failed:{exc}"
                    actual_mode = "fts"
                    # Mark vector service as degraded so subsequent requests skip it
                    mark_vector_service_degraded(str(exc))
                    if not fts_rows:
                        fts_rows = _text_candidates(
                            db, query=query, project_id=project_id, store_id=store_id, limit=limit,
                        )

    merged, rerank_applied = _merge_candidates(
        query=query,
        fts_rows=fts_rows,
        vector_rows=vector_rows,
        actual_mode=actual_mode,
        degraded=degraded,
        degradation_reason=degradation_reason,
        project_id=project_id,
        context=context,
    )
    total = len(merged)
    start = (page - 1) * page_size
    end = start + page_size
    page_rows = merged[start:end]
    stale_count = sum(1 for row in merged if row.get("stale"))

    return MemorySearchOutput(
        rows=page_rows,
        total=total,
        search_mode=actual_mode,
        degraded=degraded,
        degradation_reason=degradation_reason,
        stale_count=stale_count,
        rerank_applied=rerank_applied,
    )
