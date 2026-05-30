"""Knowledge API routes (P3-05 / P3-06).

Endpoints
---------
* ``POST   /api/v4/knowledge/documents`` — Create a knowledge document
* ``GET    /api/v4/knowledge/documents`` — List documents (paginated, filterable)
* ``GET    /api/v4/knowledge/documents/{id}`` — Get document detail
* ``PATCH  /api/v4/knowledge/documents/{id}`` — Update document
* ``POST   /api/v4/knowledge/documents/{id}/archive`` — Archive document
* ``POST   /api/v4/knowledge/documents/{id}/blocks`` — Add a block
* ``GET    /api/v4/knowledge/documents/{id}/blocks`` — List blocks
* ``PATCH  /api/v4/knowledge/blocks/{id}`` — Update a block
* ``DELETE /api/v4/knowledge/blocks/{id}`` — Delete a block
* ``POST   /api/v4/knowledge/documents/{id}/rechunk`` — Rechunk document (P3-06)
* ``GET    /api/v4/knowledge/documents/{id}/chunks`` — List chunks (P3-06)

Every write endpoint requires an ``Idempotency-Key`` header.
"""

from __future__ import annotations

from typing import Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query
from fastapi.encoders import jsonable_encoder
from sqlalchemy.orm import Session

from mneme.api.auth import get_current_user_session
from mneme.api.context import (
    ActorContext,
    RequestContext,
    get_request_context,
    with_actor,
)
from mneme.api.errors import ApiError
from mneme.api.schemas import envelope
from mneme.db.auth import AuthenticatedSession
from mneme.db.base import get_db
from mneme.db.idempotency import check_idempotency_key_any
from mneme.db.knowledge import (
    add_block,
    archive_document,
    clear_chunks,
    create_document,
    delete_block,
    get_block,
    get_document,
    insert_chunks,
    list_blocks_by_document,
    list_chunks_by_document,
    list_documents,
    stale_index_on_block_update,
    update_block,
    update_document,
)
from mneme.db.projects import get_project
from mneme.schemas import (
    ChunkingStrategy,
    KnowledgeBlockCreate,
    KnowledgeBlockRead,
    KnowledgeBlockUpdate,
    KnowledgeChunkRead,
    KnowledgeDocumentCreate,
    KnowledgeDocumentRead,
    KnowledgeDocumentUpdate,
    PaginatedData,
    PageInfo,
    RechunkRequest,
    ResponseEnvelope,
)

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


# ═══════════════════════════════════════════════════════════════════
# Request helpers
# ═══════════════════════════════════════════════════════════════════

def _require_project(db: Session, project_id: UUID) -> None:
    """Validate that a project exists (raises 404 if not)."""
    project = get_project(db, project_id)
    if project is None:
        raise ApiError(404, "not_found", f"项目 {project_id} 不存在")


def _require_document(db: Session, document_id: UUID) -> KnowledgeDocumentRead:
    """Fetch and validate that a document exists."""
    doc = get_document(db, document_id)
    if doc is None:
        raise ApiError(404, "not_found", f"知识文档 {document_id} 不存在")
    return doc


def _require_block(db: Session, block_id: UUID) -> KnowledgeBlockRead:
    """Fetch and validate that a block exists."""
    block = get_block(db, block_id)
    if block is None:
        raise ApiError(404, "not_found", f"知识块 {block_id} 不存在")
    return block


def _guard_idempotency(db: Session, key: str, expected_type: str) -> None:
    """Reject a request if the idempotency key was used for a different aggregate type."""
    existing = check_idempotency_key_any(db, idempotency_key=key)
    if existing is None:
        return
    _event_id, actual_type, _aggregate_id = existing
    if actual_type != expected_type:
        raise ApiError(
            409,
            "idempotency_conflict",
            f"幂等键已被用于 '{actual_type}'，而非 '{expected_type}'",
            details={
                "expected_aggregate_type": expected_type,
                "existing_aggregate_type": actual_type,
            },
        )


def _resolve_actor(auth: AuthenticatedSession, context: RequestContext) -> RequestContext:
    """Wire the authenticated user into the request context's actor."""
    return with_actor(
        context,
        actor=ActorContext(
            actor_type="user",
            actor_id=auth.user.user_id,
            auth_context_type="user_session",
            auth_context_id=auth.session.session_id,
        ),
    )


# ═══════════════════════════════════════════════════════════════════
# POST /api/v4/knowledge/documents — Create document
# ═══════════════════════════════════════════════════════════════════

@router.post("/documents", response_model=ResponseEnvelope[KnowledgeDocumentRead])
def create_document_route(
    payload: KnowledgeDocumentCreate,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Create a knowledge document.

    Requires ``Idempotency-Key`` header.
    Optionally link to a source_asset via ``source_asset_id``.
    """
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "写请求必须提供 Idempotency-Key 头")

    _guard_idempotency(db, context.idempotency_key, "knowledge_document")
    _require_project(db, payload.project_id)
    context = _resolve_actor(auth, context)

    # Get project_code for canonical_uri
    project = get_project(db, payload.project_id)
    project_code = project.project_code if project else ""

    doc = create_document(db, context, payload=payload, project_code=project_code)

    data = jsonable_encoder(doc)
    return envelope(data, request_id=context.request_id, correlation_id=context.correlation_id)


# ═══════════════════════════════════════════════════════════════════
# GET /api/v4/knowledge/documents — List documents
# ═══════════════════════════════════════════════════════════════════

@router.get("/documents", response_model=ResponseEnvelope[PaginatedData[KnowledgeDocumentRead]])
def list_documents_route(
    project_id: Optional[UUID] = Query(None, description="按项目过滤"),
    status: Optional[str] = Query(None, description="按状态过滤 (active/archived/deleted)"),
    sub_library_id: Optional[UUID] = Query(None, description="按子库过滤"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(50, ge=1, le=200, description="每页数量"),
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """List knowledge documents with optional filters and pagination."""
    items, total = list_documents(
        db,
        project_id=project_id,
        status=status,
        sub_library_id=sub_library_id,
        page=page,
        page_size=page_size,
    )

    total_pages = (total + page_size - 1) // page_size if total > 0 else 0

    paginated = PaginatedData(
        items=items,
        page_info=PageInfo(
            page=page,
            page_size=page_size,
            total_items=total,
            total_pages=total_pages,
            has_next=page < total_pages,
            has_previous=page > 1,
        ),
    )

    data = jsonable_encoder(paginated)
    return envelope(data, request_id=context.request_id, correlation_id=context.correlation_id)


# ═══════════════════════════════════════════════════════════════════
# GET /api/v4/knowledge/documents/{document_id} — Get document
# ═══════════════════════════════════════════════════════════════════

@router.get("/documents/{document_id}", response_model=ResponseEnvelope[KnowledgeDocumentRead])
def get_document_route(
    document_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """Get the details of a single knowledge document."""
    doc = _require_document(db, document_id)
    data = jsonable_encoder(doc)
    return envelope(data, request_id=context.request_id, correlation_id=context.correlation_id)


# ═══════════════════════════════════════════════════════════════════
# PATCH /api/v4/knowledge/documents/{document_id} — Update document
# ═══════════════════════════════════════════════════════════════════

@router.patch("/documents/{document_id}", response_model=ResponseEnvelope[KnowledgeDocumentRead])
def update_document_route(
    document_id: UUID,
    payload: KnowledgeDocumentUpdate,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Update a knowledge document's mutable fields (title, sensitivity, summary)."""
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "写请求必须提供 Idempotency-Key 头")

    _guard_idempotency(db, context.idempotency_key, "knowledge_document")
    _require_document(db, document_id)
    context = _resolve_actor(auth, context)

    try:
        doc = update_document(db, context, document_id=document_id, payload=payload)
    except ValueError as e:
        raise ApiError(400, "bad_request", str(e))

    data = jsonable_encoder(doc)
    return envelope(data, request_id=context.request_id, correlation_id=context.correlation_id)


# ═══════════════════════════════════════════════════════════════════
# POST /api/v4/knowledge/documents/{document_id}/archive — Archive
# ═══════════════════════════════════════════════════════════════════

@router.post("/documents/{document_id}/archive", response_model=ResponseEnvelope[KnowledgeDocumentRead])
def archive_document_route(
    document_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Archive a knowledge document (active → archived)."""
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "写请求必须提供 Idempotency-Key 头")

    _guard_idempotency(db, context.idempotency_key, "knowledge_document")
    _require_document(db, document_id)
    context = _resolve_actor(auth, context)

    try:
        doc = archive_document(db, context, document_id=document_id)
    except ValueError as e:
        raise ApiError(400, "bad_request", str(e))

    data = jsonable_encoder(doc)
    return envelope(data, request_id=context.request_id, correlation_id=context.correlation_id)


# ═══════════════════════════════════════════════════════════════════
# POST /api/v4/knowledge/documents/{document_id}/blocks — Add block
# ═══════════════════════════════════════════════════════════════════

@router.post("/documents/{document_id}/blocks", response_model=ResponseEnvelope[KnowledgeBlockRead])
def add_block_route(
    document_id: UUID,
    payload: KnowledgeBlockCreate,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Add a block to a knowledge document.

    ``block_key``, ``content_text``, and ``token_count`` are automatically
    generated if not provided. The document's index_state is marked stale.
    """
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "写请求必须提供 Idempotency-Key 头")

    _guard_idempotency(db, context.idempotency_key, "knowledge_block")
    _require_document(db, document_id)
    context = _resolve_actor(auth, context)

    try:
        block = add_block(db, context, document_id=document_id, payload=payload)
    except ValueError as e:
        raise ApiError(400, "bad_request", str(e))

    data = jsonable_encoder(block)
    return envelope(data, request_id=context.request_id, correlation_id=context.correlation_id)


# ═══════════════════════════════════════════════════════════════════
# GET /api/v4/knowledge/documents/{document_id}/blocks — List blocks
# ═══════════════════════════════════════════════════════════════════

@router.get("/documents/{document_id}/blocks", response_model=ResponseEnvelope[list[KnowledgeBlockRead]])
def list_blocks_route(
    document_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """List all blocks for a document, ordered by block_order ASC."""
    _require_document(db, document_id)
    blocks = list_blocks_by_document(db, document_id)
    data = jsonable_encoder(blocks)
    return envelope(data, request_id=context.request_id, correlation_id=context.correlation_id)


# ═══════════════════════════════════════════════════════════════════
# PATCH /api/v4/knowledge/blocks/{block_id} — Update block
# ═══════════════════════════════════════════════════════════════════

@router.patch("/blocks/{block_id}", response_model=ResponseEnvelope[KnowledgeBlockRead])
def update_block_route(
    block_id: UUID,
    payload: KnowledgeBlockUpdate,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Update a knowledge block's content or type.

    ``content_text`` and ``token_count`` are recalculated if ``content_markdown``
    changes. The parent document's index_state is marked stale.
    """
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "写请求必须提供 Idempotency-Key 头")

    _guard_idempotency(db, context.idempotency_key, "knowledge_block")
    _require_block(db, block_id)
    context = _resolve_actor(auth, context)

    try:
        block = update_block(db, context, block_id=block_id, payload=payload)
    except ValueError as e:
        raise ApiError(400, "bad_request", str(e))

    data = jsonable_encoder(block)
    return envelope(data, request_id=context.request_id, correlation_id=context.correlation_id)


# ═══════════════════════════════════════════════════════════════════
# DELETE /api/v4/knowledge/blocks/{block_id} — Delete block
# ═══════════════════════════════════════════════════════════════════

@router.delete("/blocks/{block_id}", response_model=ResponseEnvelope[dict])
def delete_block_route(
    block_id: UUID,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Delete a knowledge block.

    The parent document's index_state is marked stale. The delete is
    recorded in the audit trail.
    """
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "写请求必须提供 Idempotency-Key 头")

    _guard_idempotency(db, context.idempotency_key, "knowledge_block")
    _require_block(db, block_id)
    context = _resolve_actor(auth, context)

    deleted = delete_block(db, context, block_id=block_id)
    if not deleted:
        raise ApiError(404, "not_found", f"知识块 {block_id} 不存在")

    data = jsonable_encoder({"deleted": True, "block_id": str(block_id)})
    return envelope(data, request_id=context.request_id, correlation_id=context.correlation_id)


# ═══════════════════════════════════════════════════════════════════
# P3-06: Chunking routes
# ═══════════════════════════════════════════════════════════════════

@router.get("/documents/{document_id}/chunks", response_model=ResponseEnvelope[list[KnowledgeChunkRead]])
def list_chunks_route(
    document_id: UUID,
    document_version: Optional[int] = Query(None, description="按版本过滤"),
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
) -> dict:
    """List all chunks for a document, ordered by chunk_order ASC.

    Optionally filter by ``document_version`` to see chunks from a specific version.
    """
    _require_document(db, document_id)
    chunks = list_chunks_by_document(
        db, document_id=document_id, document_version=document_version
    )
    data = jsonable_encoder(chunks)
    return envelope(data, request_id=context.request_id, correlation_id=context.correlation_id)


@router.post("/documents/{document_id}/rechunk", response_model=ResponseEnvelope[list[KnowledgeChunkRead]])
def rechunk_document_route(
    document_id: UUID,
    payload: RechunkRequest,
    db: Session = Depends(get_db),
    context: RequestContext = Depends(get_request_context),
    auth: AuthenticatedSession = Depends(get_current_user_session),
) -> dict:
    """Rechunk a document using the specified strategy.

    This deletes all existing chunks for the document and regenerates them
    from the current blocks. Source maps (block→chunk) are also recreated.

    Requires ``Idempotency-Key`` header.
    """
    if not context.idempotency_key:
        raise ApiError(400, "bad_request", "写请求必须提供 Idempotency-Key 头")

    doc = _require_document(db, document_id)
    context = _resolve_actor(auth, context)

    # Fetch blocks
    blocks = list_blocks_by_document(db, document_id)
    if not blocks:
        raise ApiError(400, "bad_request", f"文档 {document_id} 没有内容块，无法分块")

    # Run chunking engine
    from mneme.knowledge import chunk_document

    block_dicts = [
        {
            "block_id": b.block_id,
            "block_order": b.block_order,
            "content_markdown": b.content_markdown,
        }
        for b in blocks
    ]

    chunk_size = payload.chunk_size or 1200
    overlap = payload.overlap or 200

    result = chunk_document(
        document_id=document_id,
        document_version=doc.current_version,
        blocks=block_dicts,
        strategy=ChunkingStrategy(payload.strategy.value),
        chunk_size=chunk_size,
        overlap=overlap,
    )

    # Persist: clear old chunks, insert new ones
    doc_full = get_document(db, document_id)

    chunk_dicts = [
        {
            "chunk_id": uuid4(),
            "chunk_order": c.chunk_order,
            "chunk_text": c.chunk_text,
            "token_count": c.token_count,
            "block_id": c.block_id,
            "document_version": doc.current_version,
            "span_start": c.span_start,
            "span_end": c.span_end,
        }
        for c in result.chunks
    ]

    # Write in a transaction
    from mneme.db.audit import (
        AuditEvent,
        OutboxEvent,
        write_with_audit_outbox_idempotency,
    )

    outbox_event = OutboxEvent(
        event_type="knowledge_document.rechunked",
        aggregate_type="knowledge_document",
        aggregate_id=document_id,
        aggregate_version=doc.current_version,
        idempotency_key=context.idempotency_key or str(uuid4()),
        producer="mneme-api",
        payload_json={
            "document_id": str(document_id),
            "strategy": payload.strategy.value,
            "chunk_count": len(chunk_dicts),
            "document_version": doc.current_version,
        },
        visibility="internal",
        publish_state="pending",
    )

    audit_event = AuditEvent(
        action="knowledge_document.rechunk",
        result="success",
        object_type="knowledge_document",
        object_id=document_id,
        project_id=doc_full.project_id if doc_full else None,
        sensitivity_level=doc.sensitivity_level.value,
        diff_summary={
            "strategy": payload.strategy.value,
            "chunk_count": len(chunk_dicts),
        },
    )

    def _do_rechunk(db2: Session) -> list[KnowledgeChunkRead]:
        clear_chunks(db2, document_id=document_id)
        chunks = insert_chunks(
            db2,
            chunks=chunk_dicts,
            project_id=doc_full.project_id,
            document_id=document_id,
        )
        # Mark index as stale so FTS gets rebuilt
        stale_index_on_block_update(db2, document_id)
        return chunks

    def _resolve_existing(_db: Session, _aggregate_id: UUID) -> list[KnowledgeChunkRead]:
        return list_chunks_by_document(_db, document_id=document_id)

    chunks = write_with_audit_outbox_idempotency(
        db,
        context,
        work=_do_rechunk,
        audit_event=audit_event,
        outbox_event=outbox_event,
        resolve_existing=_resolve_existing,
    )

    data = jsonable_encoder(chunks)
    return envelope(data, request_id=context.request_id, correlation_id=context.correlation_id)
