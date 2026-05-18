"""L7-03 Memory Dependency Graph — trigger management and utilities.

The actual graph-node sync is handled by PostgreSQL triggers defined in
migration 0019.  This module provides:

1. **Application-side helpers** — for manual sync, backfill, or repair.
2. **Trigger log queries** — read ``graph_trigger_log`` for observability.
3. **Dependency discovery** — find which memories depend on a given memory
   via ``graph_nodes`` and ``graph_edges``.

Architecture
------------
* ``memories`` INSERT/UPDATE/DELETE → PostgreSQL trigger ``fn_sync_memory_to_graph()``
  → upserts/deletes ``graph_nodes`` rows.
* ``graph_trigger_log`` records every action the trigger takes for audit.
* Application code should NOT directly write graph_nodes for memories —
  the trigger handles that.  Application code writes graph_edges separately
  via the existing graph_edges API.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from mneme.schemas.events import (
    GraphTriggerEvent,
    GraphTriggerAction,
    GraphTriggerLogEntryRead,
)

import logging
logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# SQL Queries
# ═══════════════════════════════════════════════════════════════════════

_SELECT_TRIGGER_LOG = text(r"""
    SELECT
        trigger_log_id, trigger_event, memory_id,
        node_id, edge_id, action, details_json,
        created_at
    FROM graph_trigger_log
    WHERE (:memory_id IS NULL OR memory_id = :memory_id)
      AND (:trigger_event IS NULL OR trigger_event = :trigger_event)
      AND (:action IS NULL OR action = :action)
    ORDER BY created_at DESC
    LIMIT :limit OFFSET :offset
""")

_COUNT_TRIGGER_LOG = text(r"""
    SELECT COUNT(*) FROM graph_trigger_log
    WHERE (:memory_id IS NULL OR memory_id = :memory_id)
      AND (:trigger_event IS NULL OR trigger_event = :trigger_event)
      AND (:action IS NULL OR action = :action)
""")

_SELECT_DEPENDENTS = text(r"""
    SELECT DISTINCT gn.node_id, gn.node_label, gn.node_type,
           gn.properties_json->>'memory_title' AS memory_title,
           ge.edge_type, ge.edge_label, ge.weight
    FROM graph_nodes gn
    JOIN graph_edges ge
        ON (ge.from_node_id = gn.node_id OR ge.to_node_id = gn.node_id)
    JOIN graph_nodes gn_source
        ON (ge.from_node_id = gn_source.node_id OR ge.to_node_id = gn_source.node_id)
    WHERE gn_source.source_type = 'memory'
      AND gn_source.source_id = :memory_id
      AND gn.node_id != gn_source.node_id
      AND gn.status = 'active'
      AND ge.relation_status = 'active'
    ORDER BY ge.weight DESC NULLS LAST
    LIMIT :limit
""")


# ═══════════════════════════════════════════════════════════════════════
# Query APIs
# ═══════════════════════════════════════════════════════════════════════


def list_trigger_log(
    db: Session,
    *,
    memory_id: UUID | None = None,
    trigger_event: str | None = None,
    action: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[GraphTriggerLogEntryRead], int]:
    """Read graph trigger audit log entries."""
    offset = (max(page, 1) - 1) * page_size
    params = {
        "memory_id": memory_id,
        "trigger_event": trigger_event,
        "action": action,
        "limit": page_size,
        "offset": offset,
    }
    total = db.execute(_COUNT_TRIGGER_LOG, params).scalar_one()
    rows = db.execute(_SELECT_TRIGGER_LOG, params).all()
    items = []
    for row in rows:
        data = dict(row._mapping)
        if isinstance(data.get("details_json"), str):
            import json
            data["details_json"] = json.loads(data["details_json"])
        elif data.get("details_json") is None:
            data["details_json"] = {}
        items.append(GraphTriggerLogEntryRead.model_validate(data))
    return items, total or 0


def find_memory_dependents(
    db: Session,
    *,
    memory_id: UUID,
    max_results: int = 50,
) -> list[dict]:
    """Find graph-dependant nodes for a given memory.

    Returns nodes connected via graph_edges to the graph_node that
    represents *memory_id*.
    """
    rows = db.execute(
        _SELECT_DEPENDENTS,
        {"memory_id": memory_id, "limit": max_results},
    ).mappings().all()
    return [dict(r) for r in rows]


# ═══════════════════════════════════════════════════════════════════════
# Maintenance / Repair
# ═══════════════════════════════════════════════════════════════════════


def backfill_memory_to_graph(
    db: Session,
    *,
    memory_id: UUID,
) -> dict:
    """Manually sync a single memory to Graph Nodes.

    This can be used to backfill memories that existed before the trigger
    was installed, or to repair a node that got out of sync.

    Returns the upsert result or error.
    """
    # Fetch the memory row
    row = db.execute(
        text("SELECT * FROM memories WHERE memory_id = :memory_id"),
        {"memory_id": memory_id},
    ).mappings().first()

    if row is None:
        return {"success": False, "error": f"memory {memory_id} not found"}

    mem = dict(row)
    import hashlib, json

    content_hash = hashlib.sha256(
        str(mem.get("canonical_key", memory_id)).encode("utf-8")
    ).hexdigest()

    properties = {
        "memory_title": mem.get("title"),
        "memory_status": mem.get("status"),
        "memory_decay_state": mem.get("decay_state"),
        "memory_decay_score": mem.get("decay_score"),
        "canonical_key": mem.get("canonical_key"),
    }

    sensitivity = mem.get("sensitivity_level", "normal")
    node_status = "active" if mem.get("status") == "active" else "archived"
    title = mem.get("title") or f"Memory {str(memory_id)[:8]}"

    node_row = db.execute(
        text("""
            INSERT INTO graph_nodes
                (project_id, node_type, node_label, node_key,
                 source_type, source_id, content_hash,
                 properties_json, sensitivity_level, status)
            VALUES
                (:project_id, 'memory', :node_label, :node_key,
                 'memory', :source_id, :content_hash,
                 :properties_json, :sensitivity_level, :status)
            ON CONFLICT (project_id, node_key)
            DO UPDATE SET
                node_label        = :node_label,
                properties_json   = :properties_json,
                sensitivity_level = :sensitivity_level,
                status            = :status,
                content_hash      = :content_hash,
                updated_at        = now()
            RETURNING node_id
        """),
        {
            "project_id": mem.get("project_id"),
            "node_label": title,
            "node_key": f"memory_{memory_id}",
            "source_id": memory_id,
            "content_hash": content_hash,
            "properties_json": json.dumps(properties),
            "sensitivity_level": sensitivity,
            "status": node_status,
        },
    ).first()

    if node_row is None:
        return {"success": False, "error": "upsert returned no row"}

    return {
        "success": True,
        "node_id": str(node_row[0]),
        "memory_id": str(memory_id),
        "action": "backfilled",
    }


def backfill_all_active_memories(
    db: Session,
    *,
    batch_size: int = 100,
) -> dict:
    """Backfill all active memories into graph_nodes.

    This is a one-time repair/initialization operation for existing databases
    that were created before the memory→graph trigger was installed.
    """
    total_synced = 0
    total_errors = 0
    offset = 0

    while True:
        rows = db.execute(
            text("""
                SELECT memory_id FROM memories
                WHERE status = 'active'
                ORDER BY memory_id
                LIMIT :limit OFFSET :offset
            """),
            {"limit": batch_size, "offset": offset},
        ).all()

        if not rows:
            break

        for (memory_id,) in rows:
            try:
                result = backfill_memory_to_graph(db, memory_id=memory_id)
                if result.get("success"):
                    total_synced += 1
                else:
                    total_errors += 1
                    logger.warning("Backfill failed for %s: %s", memory_id, result.get("error"))
            except Exception as exc:
                total_errors += 1
                logger.exception("Backfill error for memory %s: %s", memory_id, exc)

        offset += batch_size

    return {
        "total_synced": total_synced,
        "total_errors": total_errors,
        "completed": True,
    }
