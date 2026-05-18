"""Agent Cards data-access layer.

Manages ``agent_cards`` and ``agent_tool_items`` tables.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Session

from mneme.api.context import RequestContext
from mneme.db.audit import AuditEvent, add_audit_event
from mneme.db.transactions import transaction
from mneme.schemas.agent_cards import (
    AgentCardCreateRequest,
    AgentCardRead,
    AgentCardUpdateRequest,
    AgentToolItemCreateRequest,
    AgentToolItemRead,
    AgentToolItemUpdateRequest,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Agent Cards CRUD
# ═══════════════════════════════════════════════════════════════════════════════

_INSERT_CARD = text("""
    INSERT INTO agent_cards (card_id, agent_id, card_type, name, description,
                             content_json, status, display_order)
    VALUES (:card_id, :agent_id, :card_type, :name, :description,
            :content_json, 'active', :display_order)
    RETURNING card_id, agent_id, card_type, name, description,
              content_json, status, display_order, created_at, updated_at
""").bindparams(
    bindparam("card_id", type_=PG_UUID(as_uuid=True)),
    bindparam("agent_id", type_=PG_UUID(as_uuid=True)),
    bindparam("content_json", type_=JSONB),
)

_SELECT_CARD_BY_ID = text("""
    SELECT c.card_id, c.agent_id, c.card_type, c.name, c.description,
           c.content_json, c.status, c.display_order, c.created_at, c.updated_at,
           COALESCE(t.tool_count, 0) AS tool_count
    FROM agent_cards c
    LEFT JOIN LATERAL (
        SELECT COUNT(*) AS tool_count
        FROM agent_tool_items ti
        WHERE ti.card_id = c.card_id AND ti.status != 'archived'
    ) t ON true
    WHERE c.card_id = :card_id
""").bindparams(bindparam("card_id", type_=PG_UUID(as_uuid=True)))

_LIST_CARDS_COUNT = text("""
    SELECT COUNT(*) FROM agent_cards WHERE status != 'archived'
""")

_LIST_CARDS = text("""
    SELECT c.card_id, c.agent_id, c.card_type, c.name, c.description,
           c.content_json, c.status, c.display_order, c.created_at, c.updated_at,
           COALESCE(t.tool_count, 0) AS tool_count
    FROM agent_cards c
    LEFT JOIN LATERAL (
        SELECT COUNT(*) AS tool_count
        FROM agent_tool_items ti
        WHERE ti.card_id = c.card_id AND ti.status != 'archived'
    ) t ON true
    WHERE c.status != 'archived'
    ORDER BY c.card_type, c.display_order, c.created_at DESC
    LIMIT :page_size OFFSET :offset
""")

_UPDATE_CARD = text("""
    UPDATE agent_cards
    SET agent_id = COALESCE(:agent_id, agent_id),
        name = COALESCE(:name, name),
        description = COALESCE(:description, description),
        content_json = COALESCE(:content_json, content_json),
        status = COALESCE(:status, status),
        display_order = COALESCE(:display_order, display_order)
    WHERE card_id = :card_id AND status != 'archived'
    RETURNING card_id, agent_id, card_type, name, description,
              content_json, status, display_order, created_at, updated_at
""").bindparams(
    bindparam("card_id", type_=PG_UUID(as_uuid=True)),
    bindparam("agent_id", type_=PG_UUID(as_uuid=True)),
    bindparam("content_json", type_=JSONB),
)

_DELETE_CARD = text("""
    UPDATE agent_cards
    SET status = 'archived'
    WHERE card_id = :card_id AND status != 'archived'
    RETURNING card_id
""").bindparams(bindparam("card_id", type_=PG_UUID(as_uuid=True)))

_LIST_CARDS_BY_TYPE = text("""
    SELECT c.card_id, c.agent_id, c.card_type, c.name, c.description,
           c.content_json, c.status, c.display_order, c.created_at, c.updated_at,
           COALESCE(t.tool_count, 0) AS tool_count
    FROM agent_cards c
    LEFT JOIN LATERAL (
        SELECT COUNT(*) AS tool_count
        FROM agent_tool_items ti
        WHERE ti.card_id = c.card_id AND ti.status != 'archived'
    ) t ON true
    WHERE c.status != 'archived' AND c.card_type = :card_type
    ORDER BY c.display_order, c.created_at DESC
""")


# ═══════════════════════════════════════════════════════════════════════════════
# Agent Tool Items CRUD
# ═══════════════════════════════════════════════════════════════════════════════

_INSERT_TOOL_ITEM = text("""
    INSERT INTO agent_tool_items (item_id, card_id, name, description, tool_type,
                                   config_json, input_schema, output_schema, status, display_order)
    VALUES (:item_id, :card_id, :name, :description, :tool_type,
            :config_json, :input_schema, :output_schema, 'active', :display_order)
    RETURNING item_id, card_id, name, description, tool_type,
              config_json, input_schema, output_schema, status, display_order,
              created_at, updated_at
""").bindparams(
    bindparam("item_id", type_=PG_UUID(as_uuid=True)),
    bindparam("card_id", type_=PG_UUID(as_uuid=True)),
    bindparam("config_json", type_=JSONB),
    bindparam("input_schema", type_=JSONB),
    bindparam("output_schema", type_=JSONB),
)

_SELECT_TOOL_ITEM_BY_ID = text("""
    SELECT item_id, card_id, name, description, tool_type,
           config_json, input_schema, output_schema, status, display_order,
           created_at, updated_at
    FROM agent_tool_items
    WHERE item_id = :item_id
""").bindparams(bindparam("item_id", type_=PG_UUID(as_uuid=True)))

_LIST_TOOL_ITEMS = text("""
    SELECT item_id, card_id, name, description, tool_type,
           config_json, input_schema, output_schema, status, display_order,
           created_at, updated_at
    FROM agent_tool_items
    WHERE card_id = :card_id AND status != 'archived'
    ORDER BY display_order, created_at DESC
""").bindparams(bindparam("card_id", type_=PG_UUID(as_uuid=True)))

_UPDATE_TOOL_ITEM = text("""
    UPDATE agent_tool_items
    SET name = COALESCE(:name, name),
        description = COALESCE(:description, description),
        tool_type = COALESCE(:tool_type, tool_type),
        config_json = COALESCE(:config_json, config_json),
        input_schema = COALESCE(:input_schema, input_schema),
        output_schema = COALESCE(:output_schema, output_schema),
        status = COALESCE(:status, status),
        display_order = COALESCE(:display_order, display_order)
    WHERE item_id = :item_id AND status != 'archived'
    RETURNING item_id, card_id, name, description, tool_type,
              config_json, input_schema, output_schema, status, display_order,
              created_at, updated_at
""").bindparams(
    bindparam("item_id", type_=PG_UUID(as_uuid=True)),
    bindparam("config_json", type_=JSONB),
    bindparam("input_schema", type_=JSONB),
    bindparam("output_schema", type_=JSONB),
)

_DELETE_TOOL_ITEM = text("""
    UPDATE agent_tool_items
    SET status = 'archived'
    WHERE item_id = :item_id AND status != 'archived'
    RETURNING item_id
""").bindparams(bindparam("item_id", type_=PG_UUID(as_uuid=True)))


# ═══════════════════════════════════════════════════════════════════════════════
# Row mapping helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _json_value(value, default=None):
    if value is None:
        return default if default is not None else {}
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return default if default is not None else {}
    return value


def _card_from_row(row) -> AgentCardRead:
    data = dict(row._mapping)
    data["content_json"] = _json_value(data.get("content_json"), {})
    data["tool_count"] = data.get("tool_count", 0)
    return AgentCardRead.model_validate(data)


def _tool_from_row(row) -> AgentToolItemRead:
    data = dict(row._mapping)
    data["config_json"] = _json_value(data.get("config_json"), {})
    data["input_schema"] = _json_value(data.get("input_schema"))
    data["output_schema"] = _json_value(data.get("output_schema"))
    return AgentToolItemRead.model_validate(data)


# ═══════════════════════════════════════════════════════════════════════════════
# Public API — Agent Cards
# ═══════════════════════════════════════════════════════════════════════════════

def create_card(
    db: Session,
    context: RequestContext,
    *,
    payload: AgentCardCreateRequest,
) -> AgentCardRead:
    card_id = uuid4()

    with transaction(db):
        row = db.execute(_INSERT_CARD, {
            "card_id": card_id,
            "agent_id": payload.agent_id,
            "card_type": payload.card_type.value,
            "name": payload.name,
            "description": payload.description,
            "content_json": payload.content_json,
            "display_order": payload.display_order,
        }).one()

        add_audit_event(db, context, AuditEvent(
            action="agent_card.create",
            result="success",
            object_type="agent_card",
            object_id=card_id,
        ))

    return _card_from_row(row)


def get_card(db: Session, card_id: UUID) -> AgentCardRead | None:
    row = db.execute(_SELECT_CARD_BY_ID, {"card_id": card_id}).first()
    if row is None:
        return None
    return _card_from_row(row)


def list_cards(
    db: Session,
    *,
    page: int = 1,
    page_size: int = 50,
    card_type: str | None = None,
) -> tuple[list[AgentCardRead], int]:
    if card_type:
        rows = db.execute(_LIST_CARDS_BY_TYPE, {"card_type": card_type}).all()
        items = [_card_from_row(r) for r in rows]
        return items, len(items)

    total = db.execute(_LIST_CARDS_COUNT).scalar_one()
    offset = (page - 1) * page_size
    rows = db.execute(_LIST_CARDS, {"page_size": page_size, "offset": offset}).all()
    items = [_card_from_row(r) for r in rows]
    return items, total


def update_card(
    db: Session,
    context: RequestContext,
    *,
    card_id: UUID,
    payload: AgentCardUpdateRequest,
) -> AgentCardRead | None:
    fields = payload.model_fields_set
    if not fields:
        return get_card(db, card_id)

    existing = get_card(db, card_id)
    if existing is None or existing.status.value == "archived":
        return None

    with transaction(db):
        row = db.execute(_UPDATE_CARD, {
            "card_id": card_id,
            "agent_id": str(payload.agent_id) if "agent_id" in fields and payload.agent_id else None,
            "name": payload.name if "name" in fields else None,
            "description": payload.description if "description" in fields else None,
            "content_json": payload.content_json if "content_json" in fields else None,
            "status": payload.status.value if "status" in fields and payload.status else None,
            "display_order": payload.display_order if "display_order" in fields else None,
        }).first()

        if row is None:
            return None

        add_audit_event(db, context, AuditEvent(
            action="agent_card.update",
            result="success",
            object_type="agent_card",
            object_id=card_id,
        ))

    return _card_from_row(row)


def archive_card(
    db: Session,
    context: RequestContext,
    *,
    card_id: UUID,
) -> bool:
    existing = get_card(db, card_id)
    if existing is None or existing.status.value == "archived":
        return False

    with transaction(db):
        row = db.execute(_DELETE_CARD, {"card_id": card_id}).first()
        if row is None:
            return False

        add_audit_event(db, context, AuditEvent(
            action="agent_card.archive",
            result="success",
            object_type="agent_card",
            object_id=card_id,
        ))

    return True


# ═══════════════════════════════════════════════════════════════════════════════
# Public API — Agent Tool Items
# ═══════════════════════════════════════════════════════════════════════════════

def create_tool_item(
    db: Session,
    context: RequestContext,
    *,
    payload: AgentToolItemCreateRequest,
) -> AgentToolItemRead:
    item_id = uuid4()

    with transaction(db):
        row = db.execute(_INSERT_TOOL_ITEM, {
            "item_id": item_id,
            "card_id": payload.card_id,
            "name": payload.name,
            "description": payload.description,
            "tool_type": payload.tool_type,
            "config_json": payload.config_json,
            "input_schema": payload.input_schema,
            "output_schema": payload.output_schema,
            "display_order": payload.display_order,
        }).one()

        add_audit_event(db, context, AuditEvent(
            action="agent_tool_item.create",
            result="success",
            object_type="agent_tool_item",
            object_id=item_id,
        ))

    return _tool_from_row(row)


def get_tool_item(db: Session, item_id: UUID) -> AgentToolItemRead | None:
    row = db.execute(_SELECT_TOOL_ITEM_BY_ID, {"item_id": item_id}).first()
    if row is None:
        return None
    return _tool_from_row(row)


def list_tool_items(db: Session, card_id: UUID) -> list[AgentToolItemRead]:
    rows = db.execute(_LIST_TOOL_ITEMS, {"card_id": card_id}).all()
    return [_tool_from_row(r) for r in rows]


def update_tool_item(
    db: Session,
    context: RequestContext,
    *,
    item_id: UUID,
    payload: AgentToolItemUpdateRequest,
) -> AgentToolItemRead | None:
    fields = payload.model_fields_set
    if not fields:
        return get_tool_item(db, item_id)

    existing = get_tool_item(db, item_id)
    if existing is None or existing.status.value == "archived":
        return None

    with transaction(db):
        row = db.execute(_UPDATE_TOOL_ITEM, {
            "item_id": item_id,
            "name": payload.name if "name" in fields else None,
            "description": payload.description if "description" in fields else None,
            "tool_type": payload.tool_type if "tool_type" in fields else None,
            "config_json": payload.config_json if "config_json" in fields else None,
            "input_schema": payload.input_schema if "input_schema" in fields else None,
            "output_schema": payload.output_schema if "output_schema" in fields else None,
            "status": payload.status.value if "status" in fields and payload.status else None,
            "display_order": payload.display_order if "display_order" in fields else None,
        }).first()

        if row is None:
            return None

        add_audit_event(db, context, AuditEvent(
            action="agent_tool_item.update",
            result="success",
            object_type="agent_tool_item",
            object_id=item_id,
        ))

    return _tool_from_row(row)


def archive_tool_item(
    db: Session,
    context: RequestContext,
    *,
    item_id: UUID,
) -> bool:
    existing = get_tool_item(db, item_id)
    if existing is None or existing.status.value == "archived":
        return False

    with transaction(db):
        row = db.execute(_DELETE_TOOL_ITEM, {"item_id": item_id}).first()
        if row is None:
            return False

        add_audit_event(db, context, AuditEvent(
            action="agent_tool_item.archive",
            result="success",
            object_type="agent_tool_item",
            object_id=item_id,
        ))

    return True
