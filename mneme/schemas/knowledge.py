"""Pydantic schemas for Knowledge Document + Block CRUD (P3-05).

knowledge_documents — hierarchical documents that own blocks.
knowledge_blocks — ordered content blocks within a document.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import Field

from mneme.schemas.common import ApiSchema, SensitivityLevel


class DocumentStatus(str, Enum):
    active = "active"
    archived = "archived"
    deleted = "deleted"


class BlockType(str, Enum):
    title = "title"
    paragraph = "paragraph"
    list = "list"
    table = "table"
    quote = "quote"
    code = "code"
    image_caption = "image_caption"
    metadata = "metadata"


# ── Knowledge Document ──────────────────────────────────────────────

class KnowledgeDocumentCreate(ApiSchema):
    project_id: UUID
    title: str = Field(min_length=1, max_length=300)
    sensitivity_level: SensitivityLevel = SensitivityLevel.normal
    summary: str | None = Field(default=None, max_length=2000)
    source_asset_id: UUID | None = None
    canonical_uri: str | None = None
    sub_library_id: UUID | None = None


class KnowledgeDocumentUpdate(ApiSchema):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    sensitivity_level: SensitivityLevel | None = None
    summary: str | None = Field(default=None, max_length=2000)
    sub_library_id: UUID | None = None


class KnowledgeDocumentRead(ApiSchema):
    document_id: UUID
    project_id: UUID | None = None
    sub_library_id: UUID | None = None
    title: str
    canonical_uri: str | None = None
    document_status: DocumentStatus
    current_version: int
    sensitivity_level: SensitivityLevel
    summary: str | None = None
    created_by_user_id: UUID | None = None
    created_at: datetime
    updated_at: datetime


# ── Knowledge Block ─────────────────────────────────────────────────

class KnowledgeBlockCreate(ApiSchema):
    block_order: int | None = Field(default=None, ge=0)
    block_type: BlockType = BlockType.paragraph
    content_markdown: str = Field(min_length=1)
    token_count: int | None = Field(default=None, ge=0)


class KnowledgeBlockUpdate(ApiSchema):
    block_type: BlockType | None = None
    content_markdown: str | None = Field(default=None, min_length=1)
    token_count: int | None = Field(default=None, ge=0)


class KnowledgeBlockRead(ApiSchema):
    block_id: UUID
    document_id: UUID
    block_key: str
    block_order: int
    current_version: int
    block_type: BlockType
    content_markdown: str
    content_text: str
    token_count: int | None = None
    created_at: datetime
    updated_at: datetime


# ── Knowledge Chunk (P3-06) ───────────────────────────────────────────

class ChunkingStrategy(str, Enum):
    paragraph = "paragraph"
    sentence = "sentence"
    fixed_size = "fixed_size"


class KnowledgeChunkRead(ApiSchema):
    chunk_id: UUID
    document_id: UUID
    block_id: UUID | None = None
    chunk_order: int
    document_version: int
    chunk_text: str
    token_count: int | None = None
    created_at: datetime
    updated_at: datetime


class KnowledgeChunkSourceMap(ApiSchema):
    """Minimal source_map info linking a chunk back to its source block."""
    source_map_id: UUID
    source_block_id: UUID | None = None
    target_chunk_id: UUID
    span: dict = Field(default_factory=dict)
    mapping_role: str = "citation"


class RechunkRequest(ApiSchema):
    strategy: ChunkingStrategy = ChunkingStrategy.paragraph
    chunk_size: int | None = Field(default=None, ge=200, le=8000)
    overlap: int | None = Field(default=None, ge=0, le=2000)


# ── FTS Search (P3-07) ───────────────────────────────────────────────

class SurroundingChunkRef(ApiSchema):
    """A neighboring chunk reference in context expansion."""
    chunk_id: UUID
    chunk_order: int
    chunk_text: str
    token_count: int
    relative_position: int  # -N before, +N after matched chunk


class RelatedMemoryRef(ApiSchema):
    """A memory entry related to a knowledge chunk."""
    memory_id: UUID
    title: str
    memory_text_preview: str
    canonical_key: str
    relevance_score: float


class KnowledgeFtsSearchResult(ApiSchema):
    """A single search result from FTS over knowledge_chunks."""
    chunk_id: UUID
    document_id: UUID
    block_id: UUID | None = None
    chunk_order: int
    chunk_text: str
    rank: float
    document_title: str
    document_uri: str | None = None
    document_sensitivity: str
    block_key: str | None = None
    block_type: str | None = None
    block_order: int | None = None
    is_stale: bool = False
    stale_reason: str | None = None
    # Context expansion (P5-03)
    surrounding_chunks: list[SurroundingChunkRef] = Field(default_factory=list)
    related_memories: list[RelatedMemoryRef] = Field(default_factory=list)


class IndexStateRead(ApiSchema):
    """Current state of all indexing dimensions for a document."""
    index_state_id: UUID
    object_type: str
    object_id: UUID
    ready_version: int
    stale_version: int
    fts_state: str
    vector_state: str
    graph_state: str
    citation_state: str
    last_refreshed_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime


# ── Citation (P3-08) ──────────────────────────────────────────────────


class CitationNodeRead(ApiSchema):
    """A single node in the provenance chain (API representation)."""
    type: str          # "asset" | "document" | "block" | "chunk"
    id: UUID
    label: str
    uri: str | None = None


class CitationRead(ApiSchema):
    """Complete provenance chain from chunk back to asset."""
    chunk_id: UUID
    chunk_text: str
    chunk_order: int
    chain: list[CitationNodeRead] = Field(default_factory=list)
    document_id: UUID | None = None
    document_title: str | None = None
    document_version: int | None = None
    is_stale: bool = False
    stale_reason: str | None = None
    created_at: datetime | None = None


class CitationListResponse(ApiSchema):
    """Paginated list of citations for a document."""
    document_id: UUID
    citations: list[CitationRead]
    total: int


class SourceMapCreate(ApiSchema):
    """Create a new source_map entry linking a source to a target."""
    project_id: UUID
    source_type: str = Field(..., pattern=r"^(asset|document|block|chunk|message|raw_event|memory_candidate|external)$")
    source_id: UUID
    target_type: str = Field(..., pattern=r"^(document|block|chunk|memory_candidate|memory|asset)$")
    target_id: UUID
    source_asset_id: UUID | None = None
    source_document_id: UUID | None = None
    source_block_id: UUID | None = None
    target_document_id: UUID | None = None
    target_block_id: UUID | None = None
    target_chunk_id: UUID | None = None
    span: dict = Field(default_factory=dict)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    mapping_role: str = Field(default="citation", pattern=r"^(citation|derived_from|extracted_from|transformed_from|attachment)$")


class SourceMapRead(ApiSchema):
    """A single source_maps row (admin/debug view)."""
    source_map_id: UUID
    project_id: UUID | None = None
    source_type: str
    source_id: UUID
    target_type: str
    target_id: UUID
    source_asset_id: UUID | None = None
    source_document_id: UUID | None = None
    source_block_id: UUID | None = None
    target_document_id: UUID | None = None
    target_block_id: UUID | None = None
    target_chunk_id: UUID | None = None
    span: dict = Field(default_factory=dict)
    confidence: float | None = None
    mapping_role: str
    created_at: datetime
    updated_at: datetime
