"""Cross-encoder re-ranking via Gateway ``rerank.execute``.

Replaces the previous weighted-hybrid scoring (0.55 × fts_rank + 0.45 × vector_rank)
with a cross-encoder model that evaluates (query, document) pairs holistically.

Design
------
* **Gateway integration**: Calls ``rerank.execute`` capability through the unified
  Gateway, meaning any provider with a rerank endpoint (Cohere, Jina, etc.) can be
  used by configuring a capability binding.
* **Fallback**: When Gateway reranking is unavailable or fails, degrades gracefully
  to a simple weighted-hybrid score as a safety net.
* **Batch processing**: Large candidate sets are split into batches to stay within
  provider limits (default 96 docs per batch).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Sequence
from uuid import UUID

from mneme.api.context import RequestContext
from mneme.gateway.call import GatewayError, get_gateway
from mneme.observability.health import (
    get_vector_service_status,
    mark_vector_service_degraded,
    reset_vector_service_state,
)

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 96  # Typical rerank API limit per call
DEFAULT_TOP_N = 50       # How many results the reranker should return
DEFAULT_FTS_WEIGHT = 0.55
DEFAULT_VECTOR_WEIGHT = 0.45


# ── Exceptions ────────────────────────────────────────────────────────────


class RerankerError(Exception):
    """Base exception for reranker failures."""


class RerankerGatewayError(RerankerError):
    """Gateway rerank call failed."""


# ── Data classes ──────────────────────────────────────────────────────────


@dataclass
class RerankCandidate:
    """A single candidate to be re-ranked."""

    id: str
    text: str
    fts_score: float = 0.0
    vector_score: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class RerankResult:
    """A scored result after re-ranking."""

    id: str
    score: float
    fts_score: float = 0.0
    vector_score: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)


# ── Cross-encoder re-ranker ──────────────────────────────────────────────


class CrossEncoderReranker:
    """Re-rank candidates using a cross-encoder model via Gateway.

    Usage::

        reranker = CrossEncoderReranker()
        results = reranker.rerank(
            query="What is Mneme?",
            candidates=[
                RerankCandidate(id="1", text="Mneme is a memory platform...", fts_score=0.8),
                RerankCandidate(id="2", text="Memory systems are complex...", fts_score=0.5),
            ],
            project_id=uuid4(),
            top_n=10,
        )
    """

    def __init__(
        self,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        top_n: int = DEFAULT_TOP_N,
        fts_weight: float = DEFAULT_FTS_WEIGHT,
        vector_weight: float = DEFAULT_VECTOR_WEIGHT,
    ) -> None:
        self.batch_size = batch_size
        self.top_n = top_n
        self.fts_weight = fts_weight
        self.vector_weight = vector_weight

    def rerank(
        self,
        query: str,
        candidates: Sequence[RerankCandidate],
        *,
        project_id: UUID | None = None,
        top_n: int | None = None,
        context: RequestContext | None = None,
    ) -> list[RerankResult]:
        """Re-rank candidates using a cross-encoder via Gateway.

        Falls back to weighted-hybrid scoring if Gateway reranking fails or
        is unavailable.

        Args:
            query: The original search query.
            candidates: Candidates gathered from FTS + vector search.
            project_id: Optional project scope for capability binding.
            top_n: Number of top results to return (defaults to self.top_n).
            context: Request context for tracing.

        Returns:
            Sorted list of :class:`RerankResult`, best first.
        """
        if not candidates:
            return []

        effective_top_n = top_n if top_n is not None else self.top_n

        # Try cross-encoder via Gateway; fall back to weighted-hybrid
        try:
            results = self._rerank_via_gateway(
                query, candidates, project_id=project_id, context=context,
            )
            if results:
                reset_vector_service_state()
                return results[:effective_top_n]
        except RerankerError as exc:
            logger.warning("Cross-encoder reranking failed, falling back to hybrid: %s", exc)

        # Fallback: weighted-hybrid scoring
        return self._weighted_hybrid_fallback(candidates, top_n=effective_top_n)

    # ── Gateway path ────────────────────────────────────────────────────

    def _rerank_via_gateway(
        self,
        query: str,
        candidates: Sequence[RerankCandidate],
        *,
        project_id: UUID | None = None,
        context: RequestContext | None = None,
    ) -> list[RerankResult]:
        """Call Gateway rerank.execute for each batch of candidates."""
        if not candidates:
            return []

        gateway = get_gateway()
        all_results: list[RerankResult] = []
        candidate_map: dict[str, RerankCandidate] = {c.id: c for c in candidates}

        # Build documents list (truncate long texts to reasonable size)
        docs: list[str] = []
        for c in candidates:
            text = c.text.strip()
            # Truncate to ~1000 chars — rerank models have input limits
            if len(text) > 1000:
                text = text[:1000]
            docs.append(text)

        # Process in batches
        for batch_start in range(0, len(docs), self.batch_size):
            batch_docs = docs[batch_start : batch_start + self.batch_size]
            batch_candidates = candidates[batch_start : batch_start + self.batch_size]

            try:
                result = gateway.call(
                    capability_code="rerank.execute",
                    params={
                        "query": query,
                        "documents": batch_docs,
                        "top_n": min(self.top_n, len(batch_docs)),
                    },
                    project_id=project_id,
                    sensitivity="private",
                    actor_type="system",
                    actor_id=None,
                    auth_context_type=context.actor.auth_context_type if context else None,
                    auth_context_id=context.actor.auth_context_id if context else None,
                    request_id=context.request_id if context else None,
                    correlation_id=context.correlation_id if context else None,
                    idempotency_key=(
                        f"rerank-{context.request_id}" if context and not context.idempotency_key else None
                    ),
                    call_type="rerank",
                )
            except GatewayError as exc:
                raise RerankerGatewayError(f"Gateway rerank failed: {exc}") from exc

            # Parse rerank response — supports Cohere-compatible format
            rerank_data = result.get("data", result)
            results_list = self._parse_rerank_response(rerank_data, batch_candidates, candidate_map)
            all_results.extend(results_list)

        # De-duplicate and sort by score descending
        seen: set[str] = set()
        unique: list[RerankResult] = []
        for r in sorted(all_results, key=lambda x: x.score, reverse=True):
            if r.id not in seen:
                seen.add(r.id)
                unique.append(r)

        return unique

    @staticmethod
    def _parse_rerank_response(
        response_data: Any,
        batch_candidates: Sequence[RerankCandidate],
        candidate_map: dict[str, RerankCandidate],
    ) -> list[RerankResult]:
        """Parse rerank provider response into RerankResult list.

        Handles common response formats:
        - Cohere: ``{"results": [{"index": 0, "relevance_score": 0.9}, ...]}``
        - Jina: ``{"results": [{"index": 0, "relevance_score": 0.9}, ...]}``
        - Generic: list of ``{"index": N, "score": S}`` or ``{"index": N, "relevance_score": S}``
        """
        results: list[RerankResult] = []

        # Extract results list from response
        if isinstance(response_data, list):
            items = response_data
        elif isinstance(response_data, dict):
            items = response_data.get("results", response_data.get("data", []))
        else:
            return results

        if not isinstance(items, (list, tuple)):
            return results

        for item in items:
            if not isinstance(item, dict):
                continue

            idx = item.get("index")
            score = item.get("relevance_score") or item.get("score")

            if idx is None or score is None:
                continue

            try:
                idx = int(idx)
                score = float(score)
            except (TypeError, ValueError):
                continue

            if 0 <= idx < len(batch_candidates):
                candidate = batch_candidates[idx]
                results.append(RerankResult(
                    id=candidate.id,
                    score=score,
                    fts_score=candidate.fts_score,
                    vector_score=candidate.vector_score,
                    meta={**candidate.meta},
                ))

        return results

    # ── Fallback path ────────────────────────────────────────────────────

    def _weighted_hybrid_fallback(
        self,
        candidates: Sequence[RerankCandidate],
        top_n: int,
    ) -> list[RerankResult]:
        """Fallback weighted-hybrid scoring when cross-encoder is unavailable."""
        results: list[RerankResult] = []
        for c in candidates:
            hybrid_score = (self.fts_weight * c.fts_score) + (self.vector_weight * c.vector_score)
            results.append(RerankResult(
                id=c.id,
                score=hybrid_score,
                fts_score=c.fts_score,
                vector_score=c.vector_score,
                meta={**c.meta},
            ))
        results.sort(key=lambda r: r.score, reverse=True)
        return results[:top_n]


# ── Module-level convenience ──────────────────────────────────────────────


_reranker: CrossEncoderReranker | None = None


def get_reranker() -> CrossEncoderReranker:
    """Return the module-level :class:`CrossEncoderReranker` singleton."""
    global _reranker
    if _reranker is None:
        _reranker = CrossEncoderReranker()
    return _reranker


def rerank_candidates(
    query: str,
    candidates: Sequence[RerankCandidate],
    *,
    project_id: UUID | None = None,
    top_n: int | None = None,
    context: RequestContext | None = None,
) -> list[RerankResult]:
    """Convenience function for one-shot re-ranking."""
    return get_reranker().rerank(
        query=query,
        candidates=candidates,
        project_id=project_id,
        top_n=top_n,
        context=context,
    )
