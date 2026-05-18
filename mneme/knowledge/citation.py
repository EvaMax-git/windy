"""Citation engine — build complete provenance chains from chunks to assets (P3-08).

Design
------
A **Citation** traces a search result (chunk) all the way back to its source
asset through the ``source_maps`` table:

    Asset ──(derived_from)──→ Document ──→ Block ──(citation)──→ Chunk

Each arrow is a row in ``source_maps`` with a specific ``mapping_role``:

* ``asset → document`` : ``mapping_role = 'derived_from'``
* ``block → chunk`` : ``mapping_role = 'citation'``

The **build_citation** function walks this chain and returns a structured
:class:`Citation` object suitable for API rendering.

Stale Detection
---------------
When a document's ``index_states.fts_state`` is ``'stale'``, citations for its
chunks are also considered stale.  Search results can include ``is_stale`` to
warn users that content may have changed since indexing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Session


# ═══════════════════════════════════════════════════════════════════
# Data classes
# ═══════════════════════════════════════════════════════════════════


@dataclass
class CitationNode:
    """A single node in the provenance chain."""
    type: str          # "asset" | "document" | "block" | "chunk"
    id: UUID
    label: str         # human-readable label (title / block_key / chunk_order)
    uri: str | None = None


@dataclass
class Citation:
    """Complete provenance chain from chunk back to asset."""
    chunk_id: UUID
    chunk_text: str
    chunk_order: int
    # Chain from leaf (chunk) to root (asset)
    chain: list[CitationNode] = field(default_factory=list)
    # Metadata
    document_id: UUID | None = None
    document_title: str | None = None
    document_version: int | None = None
    # Staleness
    is_stale: bool = False
    stale_reason: str | None = None
    # Timestamps
    created_at: datetime | None = None


@dataclass
class CitationListResult:
    """Batch result for listing all citations of a document."""
    document_id: UUID
    citations: list[Citation]
    total: int


# ═══════════════════════════════════════════════════════════════════
# SQL — single chunk citation chain
# ═══════════════════════════════════════════════════════════════════

_CHUNK_CITATION_CHAIN = text(
    """
    -- Leaf: chunk
    WITH chunk_info AS (
        SELECT
            kc.chunk_id,
            kc.chunk_text,
            kc.chunk_order,
            kc.document_id,
            kc.block_id,
            kc.document_version,
            kc.created_at
        FROM knowledge_chunks kc
        WHERE kc.chunk_id = :chunk_id
    ),
    -- Block info
    block_info AS (
        SELECT
            kb.block_id,
            kb.block_key,
            kb.block_order,
            kb.document_id
        FROM knowledge_blocks kb
        WHERE kb.block_id = (SELECT block_id FROM chunk_info WHERE chunk_info.block_id IS NOT NULL)
    ),
    -- Document info
    doc_info AS (
        SELECT
            kd.document_id,
            kd.title,
            kd.canonical_uri,
            kd.project_id
        FROM knowledge_documents kd
        WHERE kd.document_id = (SELECT document_id FROM chunk_info)
    ),
    -- Asset→Document source_map (derived_from)
    asset_source_map AS (
        SELECT
            sm.source_asset_id,
            sm.target_document_id
        FROM source_maps sm
        WHERE sm.target_type = 'document'
          AND sm.mapping_role = 'derived_from'
          AND sm.target_document_id = (SELECT document_id FROM chunk_info)
        LIMIT 1
    ),
    -- Asset info (if any)
    asset_info AS (
        SELECT
            a.asset_id,
            a.original_filename,
            a.canonical_uri
        FROM assets a
        WHERE a.asset_id = (SELECT source_asset_id FROM asset_source_map)
    ),
    -- Index state
    index_state AS (
        SELECT
            is2.fts_state,
            is2.citation_state
        FROM index_states is2
        WHERE is2.object_type = 'knowledge_document'
          AND is2.object_id = (SELECT document_id FROM chunk_info)
    )
    SELECT
        ci.chunk_id,
        ci.chunk_text,
        ci.chunk_order,
        ci.document_id,
        ci.block_id,
        ci.document_version,
        ci.created_at AS chunk_created_at,
        bi.block_key,
        bi.block_order,
        di.title AS document_title,
        di.canonical_uri AS document_uri,
        di.project_id,
        ai.asset_id,
        ai.original_filename AS asset_filename,
        ai.canonical_uri AS asset_uri,
        ix.fts_state,
        ix.citation_state
    FROM chunk_info ci
    LEFT JOIN block_info bi ON TRUE
    LEFT JOIN doc_info di ON TRUE
    LEFT JOIN asset_info ai ON TRUE
    LEFT JOIN index_state ix ON TRUE
    """
).bindparams(bindparam("chunk_id", type_=PG_UUID(as_uuid=True)))


# ═══════════════════════════════════════════════════════════════════
# SQL — batch citation listing for a document
# ═══════════════════════════════════════════════════════════════════

_CITATIONS_BY_DOCUMENT = text(
    """
    WITH doc_chunks AS (
        SELECT
            kc.chunk_id,
            kc.chunk_text,
            kc.chunk_order,
            kc.document_id,
            kc.block_id,
            kc.document_version,
            kc.created_at
        FROM knowledge_chunks kc
        WHERE kc.document_id = :document_id
        ORDER BY kc.chunk_order ASC
    ),
    blocks AS (
        SELECT
            kb.block_id,
            kb.block_key,
            kb.block_order
        FROM knowledge_blocks kb
        WHERE kb.document_id = :document_id
    ),
    document AS (
        SELECT
            kd.document_id,
            kd.title,
            kd.canonical_uri,
            kd.project_id
        FROM knowledge_documents kd
        WHERE kd.document_id = :document_id
    ),
    asset_link AS (
        SELECT
            sm.source_asset_id,
            sm.target_document_id
        FROM source_maps sm
        WHERE sm.target_type = 'document'
          AND sm.mapping_role = 'derived_from'
          AND sm.target_document_id = :document_id
        LIMIT 1
    ),
    asset AS (
        SELECT
            a.asset_id,
            a.original_filename,
            a.canonical_uri
        FROM assets a, asset_link al
        WHERE a.asset_id = al.source_asset_id
    ),
    idx AS (
        SELECT
            is2.fts_state,
            is2.citation_state
        FROM index_states is2
        WHERE is2.object_type = 'knowledge_document'
          AND is2.object_id = :document_id
    )
    SELECT
        dc.chunk_id,
        dc.chunk_text,
        dc.chunk_order,
        dc.document_id,
        dc.block_id,
        dc.document_version,
        dc.created_at AS chunk_created_at,
        b.block_key,
        b.block_order,
        d.title AS document_title,
        d.canonical_uri AS document_uri,
        a.asset_id,
        a.original_filename AS asset_filename,
        a.canonical_uri AS asset_uri,
        ix.fts_state,
        ix.citation_state
    FROM doc_chunks dc
    LEFT JOIN blocks b ON b.block_id = dc.block_id
    CROSS JOIN document d
    LEFT JOIN asset a ON TRUE
    CROSS JOIN idx ix
    """
).bindparams(bindparam("document_id", type_=PG_UUID(as_uuid=True)))


# ═══════════════════════════════════════════════════════════════════
# SQL — check index stale state for a document
# ═══════════════════════════════════════════════════════════════════

_CHECK_INDEX_STALE = text(
    """
    SELECT fts_state, citation_state
    FROM index_states
    WHERE object_type = 'knowledge_document'
      AND object_id = :document_id
    """
).bindparams(bindparam("document_id", type_=PG_UUID(as_uuid=True)))


# ═══════════════════════════════════════════════════════════════════
# SQL — enumerate source_maps for a document (debug/admin)
# ═══════════════════════════════════════════════════════════════════

_LIST_SOURCE_MAPS = text(
    """
    SELECT
        source_map_id,
        source_type,
        source_id,
        target_type,
        target_id,
        source_asset_id,
        source_document_id,
        source_block_id,
        target_document_id,
        target_block_id,
        target_chunk_id,
        span,
        mapping_role,
        created_at,
        updated_at
    FROM source_maps
    WHERE (:project_id IS NULL OR project_id = :project_id)
       OR (:document_id IS NULL OR target_document_id = :document_id)
       OR (:document_id IS NULL OR source_document_id = :document_id)
    ORDER BY created_at ASC
    """
).bindparams(
    bindparam("project_id", type_=PG_UUID(as_uuid=True)),
    bindparam("document_id", type_=PG_UUID(as_uuid=True)),
)


# ═══════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════


def build_citation(db: Session, *, chunk_id: UUID) -> Citation | None:
    """Build a full provenance citation for a single chunk.

    Walks the ``source_maps`` chain:

    1. Chunk → Block (via ``knowledge_chunks.block_id``)
    2. Block → Document (via ``knowledge_blocks.document_id``)
    3. Document → Asset (via ``source_maps`` with ``derived_from``)
    4. Check ``index_states`` for staleness

    Args:
        db: Active SQLAlchemy session.
        chunk_id: The chunk to trace.

    Returns:
        A :class:`Citation` with the complete chain, or ``None`` if the chunk
        is not found.
    """
    row = db.execute(_CHUNK_CITATION_CHAIN, {"chunk_id": chunk_id}).first()
    if row is None:
        return None

    data = dict(row._mapping)
    return _row_to_citation(data)


def list_citations(
    db: Session,
    *,
    document_id: UUID,
) -> CitationListResult:
    """List all citations for every chunk in a document.

    Returns a :class:`CitationListResult` with one :class:`Citation` per chunk,
    each containing the full provenance chain back to the source asset.

    Args:
        db: Active SQLAlchemy session.
        document_id: The document whose chunks to cite.

    Returns:
        :class:`CitationListResult` with ordered citations.
    """
    rows = db.execute(_CITATIONS_BY_DOCUMENT, {"document_id": document_id}).all()

    citations: list[Citation] = []
    for row in rows:
        data = dict(row._mapping)
        citations.append(_row_to_citation(data))

    return CitationListResult(
        document_id=document_id,
        citations=citations,
        total=len(citations),
    )


def check_stale_documents(
    db: Session,
    *,
    document_ids: list[UUID],
) -> dict[UUID, tuple[str, str]]:
    """Batch-check index staleness for multiple documents.

    Checks each document individually (avoids PostgreSQL ``ANY()`` for SQLite
    compatibility).  For large batches consider calling this from a loop
    with small slices.

    Args:
        db: Active session.
        document_ids: List of document UUIDs to check.

    Returns:
        Dict mapping ``document_id`` → ``(fts_state, citation_state)``.
        Documents without an ``index_states`` row are omitted.
    """
    if not document_ids:
        return {}

    stale_map: dict[UUID, tuple[str, str]] = {}
    for doc_id in document_ids:
        row = db.execute(
            _CHECK_INDEX_STALE,
            {"document_id": doc_id},
        ).first()
        if row is not None:
            stale_map[doc_id] = (row.fts_state, row.citation_state)

    return stale_map


def is_document_stale(db: Session, *, document_id: UUID) -> tuple[bool, str | None]:
    """Check if a document's index is stale.

    Args:
        db: Active session.
        document_id: Document UUID.

    Returns:
        Tuple of ``(is_stale, reason)``. ``reason`` is ``None`` when fresh.
    """
    row = db.execute(_CHECK_INDEX_STALE, {"document_id": document_id}).first()
    if row is None:
        return True, "no_index_state"

    fts = row.fts_state
    citation = row.citation_state

    if fts == "stale":
        return True, "fts_stale"
    if citation == "stale":
        return True, "citation_stale"
    if fts == "pending":
        return True, "fts_pending"
    if citation == "pending":
        return True, "citation_pending"
    if fts == "failed":
        return True, "fts_failed"

    return False, None


def list_source_maps(
    db: Session,
    *,
    project_id: UUID | None = None,
    document_id: UUID | None = None,
) -> list[dict[str, Any]]:
    """List source_maps for a project or document (debug/admin tool).

    Args:
        db: Active session.
        project_id: Optional project filter.
        document_id: Optional document filter.

    Returns:
        List of source_map rows as dicts.
    """
    rows = db.execute(
        _LIST_SOURCE_MAPS,
        {
            "project_id": project_id,
            "document_id": document_id,
        },
    ).all()
    return [dict(row._mapping) for row in rows]


# ═══════════════════════════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════════════════════════


def _row_to_citation(data: dict[str, Any]) -> Citation:
    """Convert a DB row to a :class:`Citation` object."""
    chain: list[CitationNode] = []

    # Leaf: chunk
    chain.append(CitationNode(
        type="chunk",
        id=data["chunk_id"],
        label=f"Chunk #{data['chunk_order']}",
    ))

    # Block
    if data.get("block_id"):
        chain.append(CitationNode(
            type="block",
            id=data["block_id"],
            label=data.get("block_key") or f"Block #{data.get('block_order', '?')}",
        ))

    # Document
    chain.append(CitationNode(
        type="document",
        id=data["document_id"],
        label=data.get("document_title") or "Untitled Document",
        uri=data.get("document_uri"),
    ))

    # Asset (if linked)
    if data.get("asset_id"):
        chain.append(CitationNode(
            type="asset",
            id=data["asset_id"],
            label=data.get("asset_filename") or "Unknown Asset",
            uri=data.get("asset_uri"),
        ))

    # Determine staleness
    is_stale = False
    stale_reason = None
    if data.get("fts_state") == "stale" or data.get("citation_state") == "stale":
        is_stale = True
        reasons = []
        if data.get("fts_state") == "stale":
            reasons.append("fts_stale")
        if data.get("citation_state") == "stale":
            reasons.append("citation_stale")
        stale_reason = ",".join(reasons)
    elif data.get("fts_state") in ("pending", "failed"):
        is_stale = True
        stale_reason = f"fts_{data['fts_state']}"

    return Citation(
        chunk_id=data["chunk_id"],
        chunk_text=data["chunk_text"] or "",
        chunk_order=data["chunk_order"],
        chain=chain,
        document_id=data["document_id"],
        document_title=data.get("document_title"),
        document_version=data.get("document_version"),
        is_stale=is_stale,
        stale_reason=stale_reason,
        created_at=data.get("chunk_created_at"),
    )
