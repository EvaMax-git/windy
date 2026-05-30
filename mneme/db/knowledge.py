"""Knowledge Document + Block CRUD with audit + outbox + idempotency + object registry.

P3-05 — knowledge_documents and knowledge_blocks.

Design
------
* ``POST /api/v4/knowledge/documents`` — create_document
* ``GET  /api/v4/knowledge/documents`` — list_documents
* ``GET  /api/v4/knowledge/documents/{id}`` — get_document
* ``PATCH /api/v4/knowledge/documents/{id}`` — update_document
* ``POST /api/v4/knowledge/documents/{id}/archive`` — archive_document
* ``POST /api/v4/knowledge/documents/{id}/blocks`` — add_block
* ``PATCH /api/v4/knowledge/blocks/{id}`` — update_block
* ``DELETE /api/v4/knowledge/blocks/{id}`` — delete_block (soft)

Every write mutation is wrapped in
:func:`mneme.db.audit.write_with_audit_outbox_idempotency`.

* ``block_key`` = ``{document_id[:8]}-b{block_order:04d}``
* ``content_text`` = stripped markdown from content_markdown
* ``token_count`` = auto-calculated when not supplied
* Block updates mark ``index_states.fts_state = 'stale'``
* Document creation optionally links to source_asset via ``source_maps``
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Session

from mneme.api.context import RequestContext
from mneme.db.audit import (
    AuditEvent,
    OutboxEvent,
    write_with_audit_outbox_idempotency,
)
from mneme.domain.objects import (
    bump_version,
    create_version,
    get_registry,
    register_object,
)
from mneme.schemas.knowledge import (
    BlockType,
    DocumentStatus,
    KnowledgeBlockCreate,
    KnowledgeBlockRead,
    KnowledgeBlockUpdate,
    KnowledgeDocumentCreate,
    KnowledgeDocumentRead,
    KnowledgeDocumentUpdate,
)


# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════

_MARKDOWN_PAT = re.compile(r"[*_~`#>|\[\]()\-!\\]+")
_URL_PAT = re.compile(r"https?://\S+")


def _strip_markdown(text: str) -> str:
    """Remove basic markdown syntax and URLs to produce plain text content_text."""
    cleaned = _MARKDOWN_PAT.sub(" ", text)
    cleaned = _URL_PAT.sub("", cleaned)  # Strip link URLs (e.g., [text](url) → text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _estimate_tokens(text: str) -> int:
    """Crude token estimation: CJK chars * 0.5 + non-CJK words * 1.3."""
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿" or "　" <= ch <= "〿")
    non_cjk = len(text) - cjk
    words = len(text.split()) if cjk == 0 else max(1, len(re.findall(r"[a-zA-Z0-9]+", text)))
    return max(1, int(cjk * 0.5 + words * 1.3))


def _block_key(document_id: UUID, block_order: int) -> str:
    """Generate block_key as {document_id[:8]}-b{block_order:04d}."""
    did_hex = str(document_id).replace("-", "")[:8]
    return f"{did_hex}-b{block_order:04d}"


# ═══════════════════════════════════════════════════════════════════
# SQL statements — knowledge_documents
# ═══════════════════════════════════════════════════════════════════

_INSERT_DOCUMENT = text(
    """
    INSERT INTO knowledge_documents (
      document_id,
      project_id,
      title,
      canonical_uri,
      document_status,
      sensitivity_level,
      summary,
      created_by_user_id,
      sub_library_id
    )
    VALUES (
      :document_id,
      :project_id,
      :title,
      :canonical_uri,
      'active',
      :sensitivity_level,
      :summary,
      :created_by_user_id,
      :sub_library_id
    )
    RETURNING
      document_id,
      project_id,
      sub_library_id,
      title,
      canonical_uri,
      document_status,
      current_version,
      sensitivity_level,
      summary,
      created_by_user_id,
      created_at,
      updated_at
    """
).bindparams(
    bindparam("document_id", type_=PG_UUID(as_uuid=True)),
    bindparam("project_id", type_=PG_UUID(as_uuid=True)),
    bindparam("created_by_user_id", type_=PG_UUID(as_uuid=True)),
)

_SELECT_DOCUMENT_BY_ID = text(
    """
    SELECT
      document_id,
      project_id,
      sub_library_id,
      title,
      canonical_uri,
      document_status,
      current_version,
      sensitivity_level,
      summary,
      created_by_user_id,
      created_at,
      updated_at
    FROM knowledge_documents
    WHERE document_id = :document_id
    """
).bindparams(bindparam("document_id", type_=PG_UUID(as_uuid=True)))

_LIST_DOCUMENTS_COUNT = text(
    """
    SELECT count(*) FROM knowledge_documents
    WHERE (:project_id IS NULL OR project_id = :project_id)
      AND (:status IS NULL OR document_status = :status)
      AND (:sub_library_id IS NULL OR sub_library_id = :sub_library_id)
    """
).bindparams(
    bindparam("project_id", type_=PG_UUID(as_uuid=True)),
    bindparam("sub_library_id", type_=PG_UUID(as_uuid=True)),
)

_LIST_DOCUMENTS = text(
    """
    SELECT
      document_id,
      project_id,
      sub_library_id,
      title,
      canonical_uri,
      document_status,
      current_version,
      sensitivity_level,
      summary,
      created_by_user_id,
      created_at,
      updated_at
    FROM knowledge_documents
    WHERE (:project_id IS NULL OR project_id = :project_id)
      AND (:status IS NULL OR document_status = :status)
      AND (:sub_library_id IS NULL OR sub_library_id = :sub_library_id)
    ORDER BY created_at DESC
    LIMIT :page_size OFFSET :offset
    """
).bindparams(
    bindparam("project_id", type_=PG_UUID(as_uuid=True)),
    bindparam("sub_library_id", type_=PG_UUID(as_uuid=True)),
)

_UPDATE_DOCUMENT = text(
    """
    UPDATE knowledge_documents
    SET title = COALESCE(:title, title),
        sensitivity_level = COALESCE(:sensitivity_level, sensitivity_level),
        summary = COALESCE(:summary, summary),
        sub_library_id = COALESCE(:sub_library_id, sub_library_id),
        current_version = current_version + 1,
        updated_at = now()
    WHERE document_id = :document_id
    RETURNING
      document_id,
      project_id,
      sub_library_id,
      title,
      canonical_uri,
      document_status,
      current_version,
      sensitivity_level,
      summary,
      created_by_user_id,
      created_at,
      updated_at
    """
).bindparams(bindparam("document_id", type_=PG_UUID(as_uuid=True)))

_ARCHIVE_DOCUMENT = text(
    """
    UPDATE knowledge_documents
    SET document_status = 'archived',
        updated_at = now()
    WHERE document_id = :document_id
      AND document_status = 'active'
    RETURNING
      document_id,
      project_id,
      title,
      canonical_uri,
      document_status,
      current_version,
      sensitivity_level,
      summary,
      created_by_user_id,
      created_at,
      updated_at
    """
).bindparams(bindparam("document_id", type_=PG_UUID(as_uuid=True)))

# ═══════════════════════════════════════════════════════════════════
# SQL statements — knowledge_blocks
# ═══════════════════════════════════════════════════════════════════

_INSERT_BLOCK = text(
    """
    INSERT INTO knowledge_blocks (
      block_id,
      document_id,
      block_key,
      block_order,
      block_type,
      content_markdown,
      content_text,
      token_count
    )
    VALUES (
      :block_id,
      :document_id,
      :block_key,
      :block_order,
      :block_type,
      :content_markdown,
      :content_text,
      :token_count
    )
    RETURNING
      block_id,
      document_id,
      block_key,
      block_order,
      current_version,
      block_type,
      content_markdown,
      content_text,
      token_count,
      created_at,
      updated_at
    """
).bindparams(
    bindparam("block_id", type_=PG_UUID(as_uuid=True)),
    bindparam("document_id", type_=PG_UUID(as_uuid=True)),
)

_SELECT_BLOCK_BY_ID = text(
    """
    SELECT
      block_id,
      document_id,
      block_key,
      block_order,
      current_version,
      block_type,
      content_markdown,
      content_text,
      token_count,
      created_at,
      updated_at
    FROM knowledge_blocks
    WHERE block_id = :block_id
    """
).bindparams(bindparam("block_id", type_=PG_UUID(as_uuid=True)))

_LIST_BLOCKS_BY_DOCUMENT = text(
    """
    SELECT
      block_id,
      document_id,
      block_key,
      block_order,
      current_version,
      block_type,
      content_markdown,
      content_text,
      token_count,
      created_at,
      updated_at
    FROM knowledge_blocks
    WHERE document_id = :document_id
    ORDER BY block_order ASC
    """
).bindparams(bindparam("document_id", type_=PG_UUID(as_uuid=True)))

_UPDATE_BLOCK = text(
    """
    UPDATE knowledge_blocks
    SET block_type = COALESCE(:block_type, block_type),
        content_markdown = COALESCE(:content_markdown, content_markdown),
        content_text = COALESCE(:content_text, content_text),
        token_count = COALESCE(:token_count, token_count),
        current_version = current_version + 1,
        updated_at = now()
    WHERE block_id = :block_id
    RETURNING
      block_id,
      document_id,
      block_key,
      block_order,
      current_version,
      block_type,
      content_markdown,
      content_text,
      token_count,
      created_at,
      updated_at
    """
).bindparams(bindparam("block_id", type_=PG_UUID(as_uuid=True)))

_DELETE_BLOCK = text(
    """
    DELETE FROM knowledge_blocks
    WHERE block_id = :block_id
    """
).bindparams(bindparam("block_id", type_=PG_UUID(as_uuid=True)))

# ═══════════════════════════════════════════════════════════════════
# SQL statements — index_states
# ═══════════════════════════════════════════════════════════════════

_UPSERT_INDEX_STALE = text(
    """
    INSERT INTO index_states (
      index_state_id,
      object_type,
      object_id,
      fts_state,
      citation_state
    )
    VALUES (
      gen_random_uuid(),
      'knowledge_document',
      :document_id,
      'stale',
      'stale'
    )
    ON CONFLICT (object_type, object_id) DO UPDATE
    SET fts_state = 'stale',
        citation_state = 'stale',
        stale_version = index_states.ready_version,
        updated_at = now()
    """
).bindparams(bindparam("document_id", type_=PG_UUID(as_uuid=True)))

# P3-07: Additional index_states management

_INIT_INDEX_STATE = text(
    """
    INSERT INTO index_states (
      index_state_id,
      object_type,
      object_id,
      fts_state,
      citation_state,
      ready_version,
      stale_version
    )
    VALUES (
      gen_random_uuid(),
      'knowledge_document',
      :document_id,
      'pending',
      'pending',
      0,
      0
    )
    ON CONFLICT (object_type, object_id) DO NOTHING
    """
).bindparams(bindparam("document_id", type_=PG_UUID(as_uuid=True)))

_MARK_FTS_READY = text(
    """
    UPDATE index_states
    SET fts_state = 'ready',
        ready_version = GREATEST(ready_version, stale_version),
        stale_version = 0,
        last_refreshed_at = now(),
        last_error = NULL,
        updated_at = now()
    WHERE object_type = 'knowledge_document'
      AND object_id = :document_id
      AND fts_state IN ('stale', 'pending')
    """
).bindparams(bindparam("document_id", type_=PG_UUID(as_uuid=True)))

_MARK_FTS_FAILED = text(
    """
    UPDATE index_states
    SET fts_state = 'failed',
        last_error = :error,
        updated_at = now()
    WHERE object_type = 'knowledge_document'
      AND object_id = :document_id
    """
).bindparams(bindparam("document_id", type_=PG_UUID(as_uuid=True)))

_SELECT_STALE_FTS_INDEXES = text(
    """
    SELECT object_id
    FROM index_states
    WHERE object_type = 'knowledge_document'
      AND fts_state = 'stale'
    ORDER BY updated_at ASC
    LIMIT 100
    """
)

_READ_INDEX_STATE = text(
    """
    SELECT
      index_state_id,
      object_type,
      object_id,
      ready_version,
      stale_version,
      fts_state,
      vector_state,
      graph_state,
      citation_state,
      last_refreshed_at,
      last_error,
      created_at,
      updated_at
    FROM index_states
    WHERE object_type = 'knowledge_document'
      AND object_id = :document_id
    """
).bindparams(bindparam("document_id", type_=PG_UUID(as_uuid=True)))


def _init_index_state(db: Session, document_id: UUID) -> None:
    """Create initial index_states row for a document (idempotent)."""
    db.execute(_INIT_INDEX_STATE, {"document_id": document_id})


def _mark_fts_ready(db: Session, document_id: UUID) -> None:
    """Transition fts_state from stale/pending to ready."""
    db.execute(_MARK_FTS_READY, {"document_id": document_id})


def _mark_fts_failed(db: Session, document_id: UUID, error: str) -> None:
    """Transition fts_state to failed with error detail."""
    db.execute(_MARK_FTS_FAILED, {"document_id": document_id, "error": error})


def _select_stale_fts_indexes(db: Session) -> list[UUID]:
    """Return document_ids whose fts_state is 'stale'."""
    rows = db.execute(_SELECT_STALE_FTS_INDEXES).all()
    return [
        row.object_id if isinstance(row.object_id, UUID) else UUID(row.object_id)
        for row in rows
    ]


def _read_index_state(db: Session, document_id: UUID) -> dict | None:
    """Read the index_states row for a document, or None."""
    row = db.execute(_READ_INDEX_STATE, {"document_id": document_id}).first()
    if row is None:
        return None
    return dict(row._mapping)


# ═══════════════════════════════════════════════════════════════════
# SQL statements — source_maps
# ═══════════════════════════════════════════════════════════════════

_INSERT_SOURCE_MAP = text(
    """
    INSERT INTO source_maps (
      source_map_id,
      project_id,
      source_type,
      source_id,
      target_type,
      target_id,
      source_asset_id,
      target_document_id,
      mapping_role
    )
    VALUES (
      gen_random_uuid(),
      :project_id,
      'asset',
      :source_asset_id,
      'document',
      :target_document_id,
      :source_asset_id,
      :target_document_id,
      'derived_from'
    )
    """
).bindparams(
    bindparam("project_id", type_=PG_UUID(as_uuid=True)),
    bindparam("source_asset_id", type_=PG_UUID(as_uuid=True)),
    bindparam("target_document_id", type_=PG_UUID(as_uuid=True)),
)


# ═══════════════════════════════════════════════════════════════════
# Row mapping
# ═══════════════════════════════════════════════════════════════════

def _document_from_row(row: Any) -> KnowledgeDocumentRead:
    data = dict(row._mapping)
    return KnowledgeDocumentRead.model_validate(data)


def _block_from_row(row: Any) -> KnowledgeBlockRead:
    data = dict(row._mapping)
    return KnowledgeBlockRead.model_validate(data)


def _idempotent_resolve_doc(db: Session, document_id: UUID) -> KnowledgeDocumentRead:
    row = db.execute(_SELECT_DOCUMENT_BY_ID, {"document_id": document_id}).first()
    if row is None:
        raise LookupError(f"document {document_id} not found during idempotent replay")
    return _document_from_row(row)


def _idempotent_resolve_block(db: Session, block_id: UUID) -> KnowledgeBlockRead:
    row = db.execute(_SELECT_BLOCK_BY_ID, {"block_id": block_id}).first()
    if row is None:
        raise LookupError(f"block {block_id} not found during idempotent replay")
    return _block_from_row(row)


# ═══════════════════════════════════════════════════════════════════
# Public API — knowledge_documents
# ═══════════════════════════════════════════════════════════════════


def create_document(
    db: Session,
    context: RequestContext,
    *,
    payload: KnowledgeDocumentCreate,
    project_code: str = "",
) -> KnowledgeDocumentRead:
    """Create a knowledge document with audit, outbox, idempotency, and object registry.

    Args:
        db: Active SQLAlchemy session.
        context: Request context (must carry idempotency_key).
        payload: Document creation data.
        project_code: Project code for canonical_uri generation.

    Returns:
        The newly created :class:`KnowledgeDocumentRead`.
    """
    document_id = uuid4()
    object_type = "document"

    canonical_uri = payload.canonical_uri or (
        f"mneme://{project_code}/knowledge/{document_id}" if project_code else None
    )

    outbox_event = OutboxEvent(
        event_type="knowledge_document.created",
        aggregate_type=object_type,
        aggregate_id=document_id,
        aggregate_version=1,
        idempotency_key=context.idempotency_key or str(uuid4()),
        producer="mneme-api",
        payload_json={
            "project_id": str(payload.project_id),
            "title": payload.title,
        },
        visibility="internal",
        publish_state="pending",
    )

    audit_event = AuditEvent(
        action="knowledge_document.create",
        result="success",
        object_type=object_type,
        object_id=document_id,
        project_id=payload.project_id,
        sensitivity_level=payload.sensitivity_level.value,
    )

    def _do_insert(db: Session) -> KnowledgeDocumentRead:
        row = db.execute(
            _INSERT_DOCUMENT,
            {
                "document_id": document_id,
                "project_id": payload.project_id,
                "title": payload.title,
                "canonical_uri": canonical_uri,
                "sensitivity_level": payload.sensitivity_level.value,
                "summary": payload.summary,
                "created_by_user_id": context.actor.actor_id if context.actor.actor_type == "user" else None,
                "sub_library_id": payload.sub_library_id,
            },
        ).one()

        # Register in object_registry
        register_object(
            db,
            object_id=document_id,
            object_type=object_type,
            project_id=payload.project_id,
            object_key=f"doc:{payload.project_id}:{document_id}",
            owner_actor_type=context.actor.actor_type,
            owner_actor_id=context.actor.actor_id,
            sensitivity_level=payload.sensitivity_level.value,
            canonical_uri=canonical_uri,
        )

        # Initialise index_states row (fts_state='pending')
        _init_index_state(db, document_id)

        # Source map if from asset
        if payload.source_asset_id is not None:
            db.execute(
                _INSERT_SOURCE_MAP,
                {
                    "project_id": payload.project_id,
                    "source_asset_id": payload.source_asset_id,
                    "target_document_id": document_id,
                },
            )

        return _document_from_row(row)

    def _post_audit(db: Session, audit_id: UUID, event_id: UUID) -> None:
        create_version(
            db,
            object_id=document_id,
            object_type=object_type,
            version=1,
            action="create",
            actor_type=context.actor.actor_type,
            actor_id=context.actor.actor_id,
            audit_id=audit_id,
            event_id=event_id,
        )

    return write_with_audit_outbox_idempotency(
        db,
        context,
        work=_do_insert,
        audit_event=audit_event,
        outbox_event=outbox_event,
        resolve_existing=_idempotent_resolve_doc,
        on_success=_post_audit,
    )


def get_document(db: Session, document_id: UUID) -> KnowledgeDocumentRead | None:
    """Look up a knowledge document by primary key."""
    row = db.execute(_SELECT_DOCUMENT_BY_ID, {"document_id": document_id}).first()
    if row is None:
        return None
    return _document_from_row(row)


def list_documents(
    db: Session,
    *,
    project_id: UUID | None = None,
    status: str | None = None,
    sub_library_id: UUID | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[KnowledgeDocumentRead], int]:
    """List knowledge documents with optional filters and pagination."""
    total = db.execute(
        _LIST_DOCUMENTS_COUNT,
        {"project_id": project_id, "status": status, "sub_library_id": sub_library_id},
    ).scalar_one()
    offset = (page - 1) * page_size
    rows = db.execute(
        _LIST_DOCUMENTS,
        {
            "project_id": project_id,
            "status": status,
            "sub_library_id": sub_library_id,
            "page_size": page_size,
            "offset": offset,
        },
    ).all()
    items = [_document_from_row(row) for row in rows]
    return items, total


def update_document(
    db: Session,
    context: RequestContext,
    *,
    document_id: UUID,
    payload: KnowledgeDocumentUpdate,
) -> KnowledgeDocumentRead:
    """Update a knowledge document's mutable fields.

    Bumps current_version in both knowledge_documents and object_registry.
    """
    doc = get_document(db, document_id)
    if doc is None:
        raise ValueError(f"document {document_id} not found")

    object_type = "document"
    registry = get_registry(db, object_id=document_id, object_type=object_type)
    if registry is None:
        raise ValueError(f"object_registry entry not found for document {document_id}")
    current_version = registry.current_version
    next_version = current_version + 1

    outbox_event = OutboxEvent(
        event_type="knowledge_document.updated",
        aggregate_type=object_type,
        aggregate_id=document_id,
        aggregate_version=next_version,
        idempotency_key=f"{context.idempotency_key or ''}:doc-update:{next_version}",
        producer="mneme-api",
        payload_json={
            "document_id": str(document_id),
            "fields": payload.model_dump(exclude_none=True, mode="json"),
        },
        visibility="internal",
        publish_state="pending",
    )

    audit_event = AuditEvent(
        action="knowledge_document.update",
        result="success",
        object_type=object_type,
        object_id=document_id,
        project_id=doc.project_id,
        sensitivity_level=doc.sensitivity_level.value,
        diff_summary=payload.model_dump(exclude_none=True, mode="json"),
    )

    def _do_update(db: Session) -> KnowledgeDocumentRead:
        row = db.execute(
            _UPDATE_DOCUMENT,
            {
                "document_id": document_id,
                "title": payload.title,
                "sensitivity_level": payload.sensitivity_level.value if payload.sensitivity_level else None,
                "summary": payload.summary,
                "sub_library_id": payload.sub_library_id,
            },
        ).one()

        bump_version(
            db,
            object_id=document_id,
            object_type=object_type,
            new_version=next_version,
        )

        return _document_from_row(row)

    def _post_audit(db: Session, audit_id: UUID, event_id: UUID) -> None:
        create_version(
            db,
            object_id=document_id,
            object_type=object_type,
            version=next_version,
            action="update",
            actor_type=context.actor.actor_type,
            actor_id=context.actor.actor_id,
            audit_id=audit_id,
            event_id=event_id,
        )

    def _resolve_existing(_db: Session, _aggregate_id: UUID) -> KnowledgeDocumentRead:
        d = get_document(_db, document_id)
        if d is None:
            raise LookupError(f"document {document_id} not found")
        return d

    return write_with_audit_outbox_idempotency(
        db,
        context,
        work=_do_update,
        audit_event=audit_event,
        outbox_event=outbox_event,
        resolve_existing=_resolve_existing,
        on_success=_post_audit,
    )


def archive_document(
    db: Session,
    context: RequestContext,
    *,
    document_id: UUID,
) -> KnowledgeDocumentRead:
    """Archive a knowledge document (active → archived)."""
    doc = get_document(db, document_id)
    if doc is None:
        raise ValueError(f"document {document_id} not found")
    if doc.document_status != DocumentStatus.active:
        raise ValueError(f"document {document_id} is not active (current: {doc.document_status.value})")

    object_type = "document"
    registry = get_registry(db, object_id=document_id, object_type=object_type)
    current_version = registry.current_version if registry else doc.current_version
    next_version = current_version + 1

    outbox_event = OutboxEvent(
        event_type="knowledge_document.archived",
        aggregate_type=object_type,
        aggregate_id=document_id,
        aggregate_version=next_version,
        idempotency_key=f"{context.idempotency_key or ''}:doc-archive:{next_version}",
        producer="mneme-api",
        payload_json={"document_id": str(document_id)},
        visibility="internal",
        publish_state="pending",
    )

    audit_event = AuditEvent(
        action="knowledge_document.archive",
        result="success",
        object_type=object_type,
        object_id=document_id,
        project_id=doc.project_id,
        sensitivity_level=doc.sensitivity_level.value,
    )

    def _do_archive(db: Session) -> KnowledgeDocumentRead:
        row = db.execute(_ARCHIVE_DOCUMENT, {"document_id": document_id}).first()
        if row is None:
            raise ValueError(f"document {document_id} could not be archived (may not be active)")

        if registry is not None:
            bump_version(db, object_id=document_id, object_type=object_type, new_version=next_version)

        return _document_from_row(row)

    def _post_audit(db: Session, audit_id: UUID, event_id: UUID) -> None:
        create_version(
            db,
            object_id=document_id,
            object_type=object_type,
            version=next_version,
            action="archive",
            actor_type=context.actor.actor_type,
            actor_id=context.actor.actor_id,
            audit_id=audit_id,
            event_id=event_id,
        )

    def _resolve_existing(_db: Session, _aggregate_id: UUID) -> KnowledgeDocumentRead:
        d = get_document(_db, document_id)
        if d is None:
            raise LookupError(f"document {document_id} not found")
        return d

    return write_with_audit_outbox_idempotency(
        db,
        context,
        work=_do_archive,
        audit_event=audit_event,
        outbox_event=outbox_event,
        resolve_existing=_resolve_existing,
        on_success=_post_audit,
    )


# ═══════════════════════════════════════════════════════════════════
# Public API — knowledge_blocks
# ═══════════════════════════════════════════════════════════════════


def add_block(
    db: Session,
    context: RequestContext,
    *,
    document_id: UUID,
    payload: KnowledgeBlockCreate,
) -> KnowledgeBlockRead:
    """Add a block to a knowledge document.

    Generates ``block_key``, ``content_text``, and ``token_count`` automatically.
    Marks the document's index_state as stale.

    Args:
        db: Active session.
        context: Request context.
        document_id: Parent document UUID.
        payload: Block creation data.

    Returns:
        The newly created :class:`KnowledgeBlockRead`.
    """
    doc = get_document(db, document_id)
    if doc is None:
        raise ValueError(f"document {document_id} not found")

    block_id = uuid4()
    object_type = "block"

    # Auto-calculate block_order if not provided
    block_order = payload.block_order
    if block_order is None:
        max_order = db.execute(
            text("SELECT COALESCE(MAX(block_order), -1) FROM knowledge_blocks WHERE document_id = :doc_id"),
            {"doc_id": document_id.hex if hasattr(document_id, 'hex') else str(document_id)},
        ).scalar()
        block_order = max_order + 1

    bk = _block_key(document_id, block_order)
    content_text = _strip_markdown(payload.content_markdown)
    token_count = payload.token_count if payload.token_count is not None else _estimate_tokens(content_text)

    outbox_event = OutboxEvent(
        event_type="knowledge_block.created",
        aggregate_type=object_type,
        aggregate_id=block_id,
        aggregate_version=1,
        idempotency_key=context.idempotency_key or str(uuid4()),
        producer="mneme-api",
        payload_json={
            "document_id": str(document_id),
            "block_key": bk,
            "block_order": block_order,
            "block_type": payload.block_type.value,
        },
        visibility="internal",
        publish_state="pending",
    )

    audit_event = AuditEvent(
        action="knowledge_block.create",
        result="success",
        object_type=object_type,
        object_id=block_id,
        project_id=doc.project_id,
        sensitivity_level=doc.sensitivity_level.value,
    )

    def _do_insert(db: Session) -> KnowledgeBlockRead:
        row = db.execute(
            _INSERT_BLOCK,
            {
                "block_id": block_id,
                "document_id": document_id,
                "block_key": bk,
                "block_order": block_order,
                "block_type": payload.block_type.value,
                "content_markdown": payload.content_markdown,
                "content_text": content_text,
                "token_count": token_count,
            },
        ).one()

        # Register in object_registry
        register_object(
            db,
            object_id=block_id,
            object_type=object_type,
            project_id=doc.project_id,
            object_key=bk,
            owner_actor_type=context.actor.actor_type,
            owner_actor_id=context.actor.actor_id,
            sensitivity_level=doc.sensitivity_level.value,
        )

        # Mark document's index as stale
        _mark_index_stale(db, document_id)

        return _block_from_row(row)

    def _post_audit(db: Session, audit_id: UUID, event_id: UUID) -> None:
        create_version(
            db,
            object_id=block_id,
            object_type=object_type,
            version=1,
            action="create",
            actor_type=context.actor.actor_type,
            actor_id=context.actor.actor_id,
            audit_id=audit_id,
            event_id=event_id,
        )

    return write_with_audit_outbox_idempotency(
        db,
        context,
        work=_do_insert,
        audit_event=audit_event,
        outbox_event=outbox_event,
        resolve_existing=_idempotent_resolve_block,
        on_success=_post_audit,
    )


def get_block(db: Session, block_id: UUID) -> KnowledgeBlockRead | None:
    """Look up a knowledge block by primary key."""
    row = db.execute(_SELECT_BLOCK_BY_ID, {"block_id": block_id}).first()
    if row is None:
        return None
    return _block_from_row(row)


def list_blocks_by_document(db: Session, document_id: UUID) -> list[KnowledgeBlockRead]:
    """List all blocks for a document, ordered by block_order ASC."""
    rows = db.execute(_LIST_BLOCKS_BY_DOCUMENT, {"document_id": document_id}).all()
    return [_block_from_row(row) for row in rows]


def update_block(
    db: Session,
    context: RequestContext,
    *,
    block_id: UUID,
    payload: KnowledgeBlockUpdate,
) -> KnowledgeBlockRead:
    """Update a knowledge block's content.

    Bumps current_version and marks parent document's index as stale.
    """
    block = get_block(db, block_id)
    if block is None:
        raise ValueError(f"block {block_id} not found")

    object_type = "block"
    registry = get_registry(db, object_id=block_id, object_type=object_type)
    current_version = registry.current_version if registry else block.current_version
    next_version = current_version + 1

    # Compute derived fields if content changed
    new_content_text = None
    new_token_count = None
    if payload.content_markdown is not None:
        new_content_text = _strip_markdown(payload.content_markdown)
        new_token_count = payload.token_count if payload.token_count is not None else _estimate_tokens(new_content_text)

    outbox_event = OutboxEvent(
        event_type="knowledge_block.updated",
        aggregate_type=object_type,
        aggregate_id=block_id,
        aggregate_version=next_version,
        idempotency_key=f"{context.idempotency_key or ''}:block-update:{next_version}",
        producer="mneme-api",
        payload_json={
            "block_id": str(block_id),
            "document_id": str(block.document_id),
            "fields": payload.model_dump(exclude_none=True, mode="json"),
        },
        visibility="internal",
        publish_state="pending",
    )

    doc = get_document(db, block.document_id)
    sensitivity = doc.sensitivity_level.value if doc else "normal"

    audit_event = AuditEvent(
        action="knowledge_block.update",
        result="success",
        object_type=object_type,
        object_id=block_id,
        project_id=doc.project_id if doc else None,
        sensitivity_level=sensitivity,
        diff_summary=payload.model_dump(exclude_none=True, mode="json"),
    )

    def _do_update(db: Session) -> KnowledgeBlockRead:
        row = db.execute(
            _UPDATE_BLOCK,
            {
                "block_id": block_id,
                "block_type": payload.block_type.value if payload.block_type else None,
                "content_markdown": payload.content_markdown,
                "content_text": new_content_text,
                "token_count": new_token_count if new_token_count is not None else (payload.token_count if payload.token_count else None),
            },
        ).one()

        if registry is not None:
            bump_version(
                db,
                object_id=block_id,
                object_type=object_type,
                new_version=next_version,
            )

        # Mark document's index as stale
        _mark_index_stale(db, block.document_id)

        return _block_from_row(row)

    def _post_audit(db: Session, audit_id: UUID, event_id: UUID) -> None:
        create_version(
            db,
            object_id=block_id,
            object_type=object_type,
            version=next_version,
            action="update",
            actor_type=context.actor.actor_type,
            actor_id=context.actor.actor_id,
            audit_id=audit_id,
            event_id=event_id,
        )

    def _resolve_existing(_db: Session, _aggregate_id: UUID) -> KnowledgeBlockRead:
        b = get_block(_db, block_id)
        if b is None:
            raise LookupError(f"block {block_id} not found")
        return b

    return write_with_audit_outbox_idempotency(
        db,
        context,
        work=_do_update,
        audit_event=audit_event,
        outbox_event=outbox_event,
        resolve_existing=_resolve_existing,
        on_success=_post_audit,
    )


def delete_block(
    db: Session,
    context: RequestContext,
    *,
    block_id: UUID,
) -> bool:
    """Delete a knowledge block (hard delete in DB, soft via audit trail).

    Marks parent document's index as stale.
    Returns True if deleted, False if not found.
    """
    block = get_block(db, block_id)
    if block is None:
        return False

    object_type = "block"
    registry = get_registry(db, object_id=block_id, object_type=object_type)
    current_version = registry.current_version if registry else block.current_version
    next_version = current_version + 1

    doc = get_document(db, block.document_id)
    sensitivity = doc.sensitivity_level.value if doc else "normal"

    outbox_event = OutboxEvent(
        event_type="knowledge_block.deleted",
        aggregate_type=object_type,
        aggregate_id=block_id,
        aggregate_version=next_version,
        idempotency_key=f"{context.idempotency_key or ''}:block-delete:{next_version}",
        producer="mneme-api",
        payload_json={
            "block_id": str(block_id),
            "document_id": str(block.document_id),
        },
        visibility="internal",
        publish_state="pending",
    )

    audit_event = AuditEvent(
        action="knowledge_block.delete",
        result="success",
        object_type=object_type,
        object_id=block_id,
        project_id=doc.project_id if doc else None,
        sensitivity_level=sensitivity,
    )

    def _do_delete(db: Session) -> KnowledgeBlockRead:
        # Mark index stale first (before delete, so we still have document_id)
        _mark_index_stale(db, block.document_id)

        db.execute(_DELETE_BLOCK, {"block_id": block_id})

        if registry is not None:
            bump_version(
                db,
                object_id=block_id,
                object_type=object_type,
                new_version=next_version,
            )

        return block  # Return the block that was deleted

    def _post_audit(db: Session, audit_id: UUID, event_id: UUID) -> None:
        create_version(
            db,
            object_id=block_id,
            object_type=object_type,
            version=next_version,
            action="delete",
            actor_type=context.actor.actor_type,
            actor_id=context.actor.actor_id,
            audit_id=audit_id,
            event_id=event_id,
        )

    def _resolve_existing(_db: Session, _aggregate_id: UUID) -> KnowledgeBlockRead:
        # Already deleted — return a sentinel
        return block

    write_with_audit_outbox_idempotency(
        db,
        context,
        work=_do_delete,
        audit_event=audit_event,
        outbox_event=outbox_event,
        resolve_existing=_resolve_existing,
        on_success=_post_audit,
    )
    return True


def stale_index_on_block_update(db: Session, document_id: UUID) -> None:
    """Explicitly mark a document's index_state as stale.

    Called by block mutators. Safe to call from any context.
    """
    _mark_index_stale(db, document_id)


def _mark_index_stale(db: Session, document_id: UUID) -> None:
    """Upsert index_states row to mark fts_state + citation_state = 'stale'."""
    db.execute(_UPSERT_INDEX_STALE, {"document_id": document_id})


# ═══════════════════════════════════════════════════════════════════
# SQL statements — knowledge_chunks (P3-06)
# ═══════════════════════════════════════════════════════════════════

_DELETE_CHUNKS_BY_DOCUMENT = text(
    """
    DELETE FROM knowledge_chunks
    WHERE document_id = :document_id
    """
).bindparams(bindparam("document_id", type_=PG_UUID(as_uuid=True)))

_INSERT_CHUNK = text(
    """
    INSERT INTO knowledge_chunks (
      chunk_id,
      document_id,
      block_id,
      chunk_order,
      document_version,
      chunk_text,
      token_count
    )
    VALUES (
      :chunk_id,
      :document_id,
      :block_id,
      :chunk_order,
      :document_version,
      :chunk_text,
      :token_count
    )
    RETURNING
      chunk_id,
      document_id,
      block_id,
      chunk_order,
      document_version,
      chunk_text,
      token_count,
      created_at,
      updated_at
    """
).bindparams(
    bindparam("chunk_id", type_=PG_UUID(as_uuid=True)),
    bindparam("document_id", type_=PG_UUID(as_uuid=True)),
    bindparam("block_id", type_=PG_UUID(as_uuid=True)),
)

_LIST_CHUNKS_BY_DOCUMENT = text(
    """
    SELECT
      chunk_id,
      document_id,
      block_id,
      chunk_order,
      document_version,
      chunk_text,
      token_count,
      created_at,
      updated_at
    FROM knowledge_chunks
    WHERE document_id = :document_id
    ORDER BY chunk_order ASC
    """
).bindparams(bindparam("document_id", type_=PG_UUID(as_uuid=True)))

_LIST_CHUNKS_BY_DOCUMENT_VERSION = text(
    """
    SELECT
      chunk_id,
      document_id,
      block_id,
      chunk_order,
      document_version,
      chunk_text,
      token_count,
      created_at,
      updated_at
    FROM knowledge_chunks
    WHERE document_id = :document_id
      AND document_version = :document_version
    ORDER BY chunk_order ASC
    """
).bindparams(
    bindparam("document_id", type_=PG_UUID(as_uuid=True)),
)

_INSERT_CHUNK_SOURCE_MAP = text(
    """
    INSERT INTO source_maps (
      source_map_id,
      project_id,
      source_type,
      source_id,
      target_type,
      target_id,
      source_block_id,
      target_document_id,
      target_chunk_id,
      span,
      mapping_role
    )
    VALUES (
      gen_random_uuid(),
      :project_id,
      'block',
      :source_block_id,
      'chunk',
      :target_chunk_id,
      :source_block_id,
      :target_document_id,
      :target_chunk_id,
      CAST(:span AS jsonb),
      'citation'
    )
    RETURNING source_map_id
    """
).bindparams(
    bindparam("project_id", type_=PG_UUID(as_uuid=True)),
    bindparam("source_block_id", type_=PG_UUID(as_uuid=True)),
    bindparam("target_document_id", type_=PG_UUID(as_uuid=True)),
    bindparam("target_chunk_id", type_=PG_UUID(as_uuid=True)),
)

_LIST_CHUNK_SOURCE_MAPS = text(
    """
    SELECT
      source_map_id,
      source_block_id,
      target_chunk_id,
      span,
      mapping_role
    FROM source_maps
    WHERE target_document_id = :document_id
      AND target_type = 'chunk'
    ORDER BY created_at ASC
    """
).bindparams(bindparam("document_id", type_=PG_UUID(as_uuid=True)))

# ═══════════════════════════════════════════════════════════════════
# Row mapping — chunks
# ═══════════════════════════════════════════════════════════════════

from mneme.schemas.knowledge import KnowledgeChunkRead, KnowledgeChunkSourceMap  # noqa: E402


def _chunk_from_row(row: Any) -> KnowledgeChunkRead:
    data = dict(row._mapping)
    return KnowledgeChunkRead.model_validate(data)


def _source_map_from_row(row: Any) -> KnowledgeChunkSourceMap:
    data = dict(row._mapping)
    return KnowledgeChunkSourceMap.model_validate(data)


# ═══════════════════════════════════════════════════════════════════
# Public API — knowledge_chunks
# ═══════════════════════════════════════════════════════════════════


def clear_chunks(db: Session, *, document_id: UUID) -> int:
    """Delete all chunks for a document. Returns number deleted."""
    result = db.execute(_DELETE_CHUNKS_BY_DOCUMENT, {"document_id": document_id})
    return result.rowcount


def insert_chunks(
    db: Session,
    *,
    chunks: list[dict[str, Any]],
    project_id: UUID,
    document_id: UUID,
) -> list[KnowledgeChunkRead]:
    """Insert chunk rows and their source_maps in one batch.

    Args:
        db: Active session.
        chunks: List of dicts with keys matching :class:`ChunkRecord`:
            ``chunk_id``, ``chunk_order``, ``chunk_text``, ``token_count``,
            ``block_id``, ``document_version``, ``span_start``, ``span_end``.
        project_id: Parent project UUID for source_map entries.
        document_id: Parent document UUID.

    Returns:
        List of inserted :class:`KnowledgeChunkRead` rows.
    """
    from mneme.knowledge.jieba_segment import segment

    results: list[KnowledgeChunkRead] = []
    for ch in chunks:
        # Segment Chinese text for FTS indexing (matches search-time segmentation)
        raw_text = ch["chunk_text"]
        segmented_text = segment(raw_text) if raw_text else raw_text
        row = db.execute(
            _INSERT_CHUNK,
            {
                "chunk_id": ch["chunk_id"],
                "document_id": document_id,
                "block_id": ch.get("block_id"),
                "chunk_order": ch["chunk_order"],
                "document_version": ch["document_version"],
                "chunk_text": segmented_text,
                "token_count": ch.get("token_count"),
            },
        ).one()
        results.append(_chunk_from_row(row))

        # Insert source_map for block→chunk when block_id is present
        if ch.get("block_id"):
            db.execute(
                _INSERT_CHUNK_SOURCE_MAP,
                {
                    "project_id": project_id,
                    "source_block_id": ch["block_id"],
                    "target_document_id": document_id,
                    "target_chunk_id": ch["chunk_id"],
                    "span": json.dumps({
                        "start": ch.get("span_start", 0),
                        "end": ch.get("span_end", 0),
                    }),
                },
            )

    return results


def list_chunks_by_document(
    db: Session,
    *,
    document_id: UUID,
    document_version: int | None = None,
) -> list[KnowledgeChunkRead]:
    """List chunks for a document, optionally filtered by version."""
    if document_version is not None:
        rows = db.execute(
            _LIST_CHUNKS_BY_DOCUMENT_VERSION,
            {"document_id": document_id, "document_version": document_version},
        ).all()
    else:
        rows = db.execute(
            _LIST_CHUNKS_BY_DOCUMENT,
            {"document_id": document_id},
        ).all()
    return [_chunk_from_row(row) for row in rows]


def list_chunk_source_maps(
    db: Session,
    *,
    document_id: UUID,
) -> list[KnowledgeChunkSourceMap]:
    """List source_maps for a document's chunks (block→chunk citations)."""
    rows = db.execute(
        _LIST_CHUNK_SOURCE_MAPS,
        {"document_id": document_id},
    ).all()
    return [_source_map_from_row(row) for row in rows]
