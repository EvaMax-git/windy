"""Pipeline registry data-access layer.

Manages the ``pipeline_registry`` table — the catalogue of available
knowledge-processing pipelines that can be selected during import.
"""

from __future__ import annotations

import json
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from mneme.db.pg_arrays import parse_pg_array, to_pg_array
from mneme.schemas.pipeline_registry import (
    PipelineRegistryCreateRequest,
    PipelineRegistryRead,
)


# ── SQL ────────────────────────────────────────────────────────────────────────

_INSERT_PIPELINE = text("""
    INSERT INTO pipeline_registry (id, name, input_formats, processor_module, accept_chunk_types, target_stores, metadata_json)
    VALUES (:id, :name, CAST(:input_formats AS text[]), :processor_module, CAST(:accept_chunk_types AS text[]), CAST(:target_stores AS text[]), :metadata_json)
    RETURNING id, name, input_formats, processor_module, accept_chunk_types, target_stores, metadata_json, created_at
""")

_SELECT_ALL = text("""
    SELECT id, name, input_formats, processor_module, accept_chunk_types, target_stores, metadata_json, created_at
    FROM pipeline_registry
    ORDER BY created_at DESC
""")

_SELECT_BY_ID = text("""
    SELECT id, name, input_formats, processor_module, accept_chunk_types, target_stores, metadata_json, created_at
    FROM pipeline_registry
    WHERE id = :id
""")

_SELECT_BY_MODULE = text("""
    SELECT id, name, input_formats, processor_module, accept_chunk_types, target_stores, metadata_json, created_at
    FROM pipeline_registry
    WHERE processor_module = :pm
""")

_DELETE_BY_ID = text("""
    DELETE FROM pipeline_registry
    WHERE id = :id
    RETURNING id
""")

_UPDATE_BY_ID = text("""
    UPDATE pipeline_registry
    SET name = COALESCE(:name, name),
        input_formats = COALESCE(CAST(:input_formats AS text[]), input_formats),
        processor_module = COALESCE(:processor_module, processor_module),
        accept_chunk_types = COALESCE(CAST(:accept_chunk_types AS text[]), accept_chunk_types),
        target_stores = COALESCE(CAST(:target_stores AS text[]), target_stores),
        metadata_json = COALESCE(CAST(:metadata_json AS jsonb), metadata_json)
    WHERE id = :id
    RETURNING id, name, input_formats, processor_module, accept_chunk_types, target_stores, metadata_json, created_at
""")


# ── Helpers (delegate to shared pg_arrays module) ───────────────────────────────

# Backward-compatible aliases — new code should import directly from
# ``mneme.db.pg_arrays``.
_to_pg_array = to_pg_array
_parse_json_array = parse_pg_array


# ── Row mapping ────────────────────────────────────────────────────────────────


def _pipeline_from_row(row) -> PipelineRegistryRead:
    data = dict(row._mapping)
    data["input_formats"] = _parse_json_array(data.get("input_formats"))
    data["accept_chunk_types"] = _parse_json_array(data.get("accept_chunk_types"))
    data["target_stores"] = _parse_json_array(data.get("target_stores"))
    data["metadata_json"] = _parse_json_for_dict(data.get("metadata_json"))
    if data.get("created_at"):
        data["created_at"] = str(data["created_at"])
    return PipelineRegistryRead.model_validate(data)


def _parse_json_for_dict(value) -> dict:
    """Parse a PostgreSQL jsonb value into a Python dict."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            result = json.loads(value)
            return result if isinstance(result, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


# ── Public API ─────────────────────────────────────────────────────────────────

def list_pipeline_registries(
    db: Session,
) -> list[PipelineRegistryRead]:
    """List all pipeline registries ordered by created_at DESC."""
    rows = db.execute(_SELECT_ALL).fetchall()
    items = [_pipeline_from_row(r) for r in rows]
    return items


def get_pipeline_registry(db: Session, pipeline_id: UUID) -> PipelineRegistryRead | None:
    """Look up a pipeline registry by ID."""
    row = db.execute(_SELECT_BY_ID, {"id": str(pipeline_id)}).fetchone()
    if row is None:
        return None
    return _pipeline_from_row(row)


def create_pipeline_registry(
    db: Session,
    payload: PipelineRegistryCreateRequest,
) -> PipelineRegistryRead:
    """Register a new pipeline. Accepts a Pydantic schema object."""
    pipeline_id = uuid4()

    row = db.execute(
        _INSERT_PIPELINE,
        {
            "id": str(pipeline_id),
            "name": payload.name,
            "input_formats": _to_pg_array(payload.input_formats or []),
            "processor_module": payload.processor_module,
            "accept_chunk_types": _to_pg_array(payload.accept_chunk_types or []),
            "target_stores": _to_pg_array(payload.target_stores or []),
            "metadata_json": json.dumps(payload.metadata_json or {}),
        },
    ).fetchone()

    db.commit()
    return _pipeline_from_row(row)


def get_pipeline_registry_by_module(db: Session, processor_module: str) -> PipelineRegistryRead | None:
    """Look up a pipeline by its processor_module key."""
    row = db.execute(_SELECT_BY_MODULE, {"pm": processor_module}).fetchone()
    if row is None:
        return None
    return _pipeline_from_row(row)


def update_pipeline_registry(
    db: Session,
    pipeline_id: UUID,
    *,
    name: str | None = None,
    input_formats: list[str] | None = None,
    processor_module: str | None = None,
    accept_chunk_types: list[str] | None = None,
    target_stores: list[str] | None = None,
    metadata_json: dict | None = None,
) -> PipelineRegistryRead | None:
    """Update a pipeline registry entry. Only non-None fields are applied."""
    row = db.execute(
        _UPDATE_BY_ID,
        {
            "id": str(pipeline_id),
            "name": name,
            "input_formats": _to_pg_array(input_formats) if input_formats is not None else None,
            "processor_module": processor_module,
            "accept_chunk_types": _to_pg_array(accept_chunk_types) if accept_chunk_types is not None else None,
            "target_stores": _to_pg_array(target_stores) if target_stores is not None else None,
            "metadata_json": json.dumps(metadata_json) if metadata_json is not None else None,
        },
    ).fetchone()

    if row is None:
        return None

    db.commit()
    return _pipeline_from_row(row)


def delete_pipeline_registry(db: Session, pipeline_id: UUID) -> PipelineRegistryRead | None:
    """Delete a pipeline registration. Returns deleted record or None if not found."""
    existing = get_pipeline_registry(db, pipeline_id)
    if existing is None:
        return None
    db.execute(_DELETE_BY_ID, {"id": str(pipeline_id)})
    db.commit()
    return existing


def delete_pipeline_registry_by_module(db: Session, processor_module: str) -> PipelineRegistryRead | None:
    """Delete a pipeline registration by processor_module. Returns the deleted record or None."""
    existing = get_pipeline_registry_by_module(db, processor_module)
    if existing is None:
        return None
    deleted = delete_pipeline_registry(db, existing.id)
    return existing if deleted else None


# ── Bootstrap / Seed ───────────────────────────────────────────────────────────

_DEFAULT_PIPELINES = [
    {
        "name": "标准分块",
        "input_formats": ["md", "txt", "office"],
        "processor_module": "chunker_standard",
        "accept_chunk_types": ["text", "code"],
        "target_stores": ["vector", "fulltext"],
    },
    {
        "name": "OCR解析",
        "input_formats": ["pdf", "image"],
        "processor_module": "ocr_parser",
        "accept_chunk_types": ["text", "image_caption"],
        "target_stores": ["vector", "fulltext", "graph"],
    },
    {
        "name": "对话解析",
        "input_formats": ["chat", "json"],
        "processor_module": "conversation_parser",
        "accept_chunk_types": ["text"],
        "target_stores": ["graph"],
    },
]

def seed_pipeline_registries(db: Session) -> list[PipelineRegistryRead]:
    """Insert default pipelines (per-module idempotent).

    Skips modules that already exist in the registry.
    Returns the list of newly-inserted PipelineRegistryRead objects.
    """
    inserted: list[PipelineRegistryRead] = []
    for p in _DEFAULT_PIPELINES:
        existing = get_pipeline_registry_by_module(db, p["processor_module"])
        if existing is not None:
            continue

        payload = PipelineRegistryCreateRequest(
            name=p["name"],
            input_formats=p["input_formats"],
            processor_module=p["processor_module"],
            accept_chunk_types=p["accept_chunk_types"],
            target_stores=p["target_stores"],
        )
        result = create_pipeline_registry(db, payload=payload)
        inserted.append(result)

    return inserted
