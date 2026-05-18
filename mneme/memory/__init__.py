"""Memory engine — FTS index, search, extract pipeline, graph relations (P4-07 / P4-09 / P4-10 / P8-01).

Public API
----------
* **Search** — ``search_memories``, ``MemorySearchOutput``
* **FTS** — ``search_fts``, ``ensure_fts_index``
* **Embedding** — ``embed_text``, ``embed_index_entry``, ``EmbeddingError``
* **Index Manager** — ``on_memory_activated``, ``on_memory_expired``, etc.
* **Extract Pipeline** — ``run_extract_pipeline``, ``MemoryExtractPipeline``
* **LLM Extract** — ``build_extract_prompt``, ``parse_extract_response``
* **Evidence Parser** — ``parse_evidence_spans``, ``EvidenceSpan``
* **Graph Relations** — ``auto_create_relations_smart``, ``auto_create_relations``, ``AutoEdgeResult``
* **PPR Traversal** — ``ppr_search``, ``run_ppr_recall``, ``PprRecallResult``
* **Temporal Cluster** — ``temporal_cluster_search``, ``TemporalClusterResult``

Interfaces (ABC)
----------------
* ``ISearchEngine`` — abstract search contract
* ``IIndexManager`` — abstract index lifecycle contract
* ``IExtractPipeline`` — abstract extract pipeline contract
"""

from mneme.memory.evidence_parser import parse_evidence_spans, EvidenceSpan
from mneme.memory.embedding import (
    EmbeddingError,
    EmbeddingGatewayError,
    EmbeddingResponseError,
    EmbeddingEntryNotFound,
    EmbeddingCallResult,
    embed_text,
    embed_index_entry,
)
from mneme.memory.fts import (
    ensure_fts_index,
    search_fts,
)
from mneme.memory.index_manager import (
    on_memory_activated,
    on_memory_expired,
    on_memory_restored,
    on_memory_deleted,
    on_memory_updated,
    _build_index_text,
    _compute_content_hash,
)
from mneme.memory.llm_extract import (
    ExtractedCandidate,
    ExtractResult,
    build_extract_prompt,
    parse_extract_response,
)
from mneme.memory.search import (
    MemorySearchOutput,
    search_memories,
)
from mneme.memory.extract_pipeline import (
    ExtractPipelineError,
    ExtractOutput,
    MemoryExtractPipeline,
    run_extract_pipeline,
)
from mneme.memory.interfaces import (
    ISearchEngine,
    IIndexManager,
    IExtractPipeline,
)
from mneme.memory.refine import run_refine_pipeline
from mneme.memory.refine.conflict import detect_conflicts
from mneme.memory.refine.dedup import detect_duplicates
from mneme.memory.refine.expire import scan_expire_candidates
from mneme.memory.refine.quality import score_memories
from mneme.memory.graph_relations import (
    auto_create_relations,
    auto_create_relations_smart,
    auto_create_relations_text,
    AutoEdgeResult,
    GraphEdgeCandidate,
)
from mneme.memory.query_rewriter import (
    RewriteResult,
    gather_context_for_rewrite,
    rewrite_query,
    quick_rewrite,
)
from mneme.memory.ppr_traversal import (
    ppr_search,
    fetch_ppr_node_details,
    run_ppr_recall,
    PprRecallResult,
)
from mneme.memory.temporal_cluster import (
    temporal_cluster_search,
    parse_temporal_expressions,
    TemporalClusterResult,
    TemporalRange,
)

__all__ = [
    # ── Evidence Parser ──
    "parse_evidence_spans",
    "EvidenceSpan",
    # ── Embedding ──
    "EmbeddingError",
    "EmbeddingGatewayError",
    "EmbeddingResponseError",
    "EmbeddingEntryNotFound",
    "EmbeddingCallResult",
    "embed_text",
    "embed_index_entry",
    # ── FTS ──
    "ensure_fts_index",
    "search_fts",
    # ── Index Manager (lifecycle hooks) ──
    "on_memory_activated",
    "on_memory_expired",
    "on_memory_restored",
    "on_memory_deleted",
    "on_memory_updated",
    "_build_index_text",
    "_compute_content_hash",
    # ── LLM Extract ──
    "ExtractedCandidate",
    "ExtractResult",
    "build_extract_prompt",
    "parse_extract_response",
    # ── Search ──
    "MemorySearchOutput",
    "search_memories",
    # ── Extract Pipeline ──
    "ExtractPipelineError",
    "ExtractOutput",
    "MemoryExtractPipeline",
    "run_extract_pipeline",
    # ── Interfaces (ABC) ──
    "ISearchEngine",
    "IIndexManager",
    "IExtractPipeline",
    # ── Refine Pipeline ──
    "run_refine_pipeline",
    "detect_duplicates",
    "detect_conflicts",
    "score_memories",
    "scan_expire_candidates",
    # ── Graph Relations ──
    "auto_create_relations",
    "auto_create_relations_smart",
    "auto_create_relations_text",
    "AutoEdgeResult",
    "GraphEdgeCandidate",
    # ── Query Rewriter ──
    "RewriteResult",
    "gather_context_for_rewrite",
    "rewrite_query",
    "quick_rewrite",
    # ── PPR Traversal ──
    "ppr_search",
    "fetch_ppr_node_details",
    "run_ppr_recall",
    "PprRecallResult",
    # ── Temporal Cluster ──
    "temporal_cluster_search",
    "parse_temporal_expressions",
    "TemporalClusterResult",
    "TemporalRange",
]
