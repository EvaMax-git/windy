"""Search aggregation module — cross-encoder reranking and context window expansion.

Core features
-------------
* :mod:`mneme.search.reranker` — Cross-encoder re-ranking via Gateway ``rerank.execute``.
* :mod:`mneme.search.context_window` — Context window expansion (chunk → surrounding paragraphs → related memories).

These modules integrate into the existing search pipeline in
:mod:`mneme.memory.search`, :mod:`mneme.knowledge.fts`, and
:mod:`mneme.api.routes.system.global_search`.
"""

from mneme.search.reranker import (
    CrossEncoderReranker,
    RerankCandidate,
    RerankResult,
    RerankerError,
    rerank_candidates,
)
from mneme.search.context_window import (
    expand_chunk_context,
    ContextWindowResult,
    ContextWindowConfig,
)

__all__ = [
    "CrossEncoderReranker",
    "RerankCandidate",
    "RerankResult",
    "RerankerError",
    "rerank_candidates",
    "expand_chunk_context",
    "ContextWindowResult",
    "ContextWindowConfig",
]
