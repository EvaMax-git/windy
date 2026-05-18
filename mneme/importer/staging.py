"""Importer staging — creates inbox_items for importer source items.

Each source item gets an inbox item with ``inbox_type='importer'``
and ``source='importer'``.  The inbox item carries the original payload
in ``payload_json`` so the pipeline consumer can reconstruct the full
import context later.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from mneme.api.context import RequestContext
from mneme.schemas.importer import ImportSourceItem


def build_inbox_payload(item: ImportSourceItem, project_id: UUID) -> dict[str, Any]:
    """Build an inbox item creation payload from a source item.

    Args:
        item: Source item from the import payload.
        project_id: Target project UUID.

    Returns:
        Dict suitable for :class:`InboxItemCreateRequest`.
    """
    return {
        "project_id": project_id,
        "inbox_type": "importer",
        "source": "importer",
        "source_uri": item.content_uri,
        "source_ref": f"mneme2:{item.legacy_id}",
        "title": item.title[:300],
        "content_hash": item.content_hash,
        "payload_json": {
            "legacy_id": item.legacy_id,
            "source_type": item.source_type.value
                if hasattr(item.source_type, "value")
                else str(item.source_type),
            "original_title": item.title,
            "content_type": item.content_type,
            "content_text": item.content_text,
            "content_uri": item.content_uri,
            "size_bytes": item.size_bytes,
            "tags": item.tags,
            "author": item.author,
            "original_created_at": item.created_at.isoformat() if item.created_at else None,
            "original_updated_at": item.updated_at.isoformat() if item.updated_at else None,
        },
        "metadata_json": item.metadata,
    }


def create_inbox_for_item(
    db: Session,
    context: RequestContext,
    *,
    item: ImportSourceItem,
    project_id: UUID,
) -> Any:
    """Create an inbox item for a single import source item.

    Reuses the existing :func:`mneme.db.inbox.create_inbox_item`.

    Args:
        db: Active session.
        context: Request context.
        item: Source item.
        project_id: Target project.

    Returns:
        The created :class:`InboxItemRead`.
    """
    from mneme.db.inbox import create_inbox_item
    from mneme.schemas.storage import InboxItemCreateRequest, InboxType

    payload_data = build_inbox_payload(item, project_id)

    inbox_payload = InboxItemCreateRequest(
        project_id=project_id,
        inbox_type=InboxType.importer,
        source="importer",
        source_uri=item.content_uri,
        source_ref=f"mneme2:{item.legacy_id}",
        title=item.title[:300] if item.title else item.legacy_id,
        content_hash=item.content_hash,
        payload_json=payload_data["payload_json"],
        metadata_json=item.metadata or {},
    )

    return create_inbox_item(
        db,
        context,
        payload=inbox_payload,
        status="received",
    )
