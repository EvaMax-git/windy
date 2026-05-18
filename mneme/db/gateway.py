"""P2-11 Gateway data-access layer — providers, provider_models, capabilities, capability_bindings.

Uses raw SQL via SQLAlchemy ``text()`` to stay aligned with DDL column names in
``0001_baseline_45_tables.py``.
"""

from __future__ import annotations

import json
import logging
from uuid import UUID, uuid4

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Session

from mneme.db.base import SessionLocal

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _row_to_dict(row, column_names: list[str]) -> dict:
    """Convert a RowMapping to a plain dict, coercing UUIDs, JSON, and booleans."""
    import json as _json_mod

    _bool_cols = {
        "supports_streaming", "supports_json_mode", "supports_tools",
        "supports_vision", "require_review", "allow_streaming",
        "retry_exhausted", "review_required",
    }
    d: dict = {}
    m = row._mapping if hasattr(row, "_mapping") else row
    for col in column_names:
        val = m.get(col)
        if isinstance(val, UUID):
            d[col] = str(val)
        elif isinstance(val, str) and col.endswith("_json"):
            try:
                parsed = _json_mod.loads(val)
                # JSON null literal → empty dict for schema compatibility
                d[col] = parsed if parsed is not None else {}
            except (_json_mod.JSONDecodeError, TypeError):
                d[col] = {} if val is None else val
        elif val is None and col.endswith("_json"):
            # SQL NULL for a JSON column → empty dict
            d[col] = {}
        elif col in _bool_cols and val is not None:
            d[col] = bool(val)
        else:
            d[col] = val
    return d


# ═══════════════════════════════════════════════════════════════════════════════
# Providers
# ═══════════════════════════════════════════════════════════════════════════════

_PROVIDER_COLS = [
    "provider_id", "provider_code", "name", "provider_type", "status",
    "endpoint_base", "config_json", "created_at", "updated_at",
]


_INSERT_PROVIDER = text("""
    INSERT INTO providers (provider_id, provider_code, name, provider_type, status, endpoint_base, config_json)
    VALUES (:provider_id, :provider_code, :name, :provider_type, :status, :endpoint_base, cast(:config_json AS JSONB))
    RETURNING provider_id
""")

_SELECT_PROVIDER_BY_ID = text("""
    SELECT provider_id, provider_code, name, provider_type, status,
           endpoint_base, config_json, created_at, updated_at
    FROM providers
    WHERE provider_id = :provider_id
""")

_SELECT_PROVIDER_BY_CODE = text("""
    SELECT provider_id, provider_code, name, provider_type, status,
           endpoint_base, config_json, created_at, updated_at
    FROM providers
    WHERE provider_code = :provider_code
    LIMIT 1
""")

_COUNT_PROVIDERS = text("""
    SELECT count(*) FROM providers
    WHERE 1=1
      AND (:provider_type IS NULL OR provider_type = :provider_type)
      AND (:status IS NULL OR status = :status)
      AND (:search IS NULL
           OR provider_code LIKE '%' || :search || '%'
           OR name LIKE '%' || :search || '%')
""")

_QUERY_PROVIDERS = text("""
    SELECT provider_id, provider_code, name, provider_type, status,
           endpoint_base, config_json, created_at, updated_at
    FROM providers
    WHERE 1=1
      AND (:provider_type IS NULL OR provider_type = :provider_type)
      AND (:status IS NULL OR status = :status)
      AND (:search IS NULL
           OR provider_code LIKE '%' || :search || '%'
           OR name LIKE '%' || :search || '%')
    ORDER BY created_at DESC
    LIMIT :limit OFFSET :offset
""")

_UPDATE_PROVIDER = text("""
    UPDATE providers
    SET name          = COALESCE(:name, name),
        provider_type = COALESCE(:provider_type, provider_type),
        status        = COALESCE(:status, status),
        endpoint_base = COALESCE(:endpoint_base, endpoint_base),
        config_json   = COALESCE(cast(:config_json AS JSONB), config_json),
        updated_at    = CURRENT_TIMESTAMP
    WHERE provider_id = :provider_id
    RETURNING provider_id
""")


def create_provider(
    *,
    provider_code: str,
    name: str,
    provider_type: str,
    status: str = "active",
    endpoint_base: str | None = None,
    config_json: dict | None = None,
) -> dict:
    """Insert a new provider row. Returns the full row as a dict."""
    provider_id = uuid4()
    with SessionLocal() as db:
        new_id = db.execute(
            _INSERT_PROVIDER,
            {
                "provider_id": provider_id,
                "provider_code": provider_code,
                "name": name,
                "provider_type": provider_type,
                "status": status,
                "endpoint_base": endpoint_base,
                "config_json": json.dumps(config_json or {}),
            },
        ).scalar_one()
        db.commit()

    return get_provider_by_id(new_id)


def get_provider_by_id(provider_id: UUID) -> dict | None:
    """Return a provider row by primary key, or None."""
    with SessionLocal() as db:
        row = db.execute(_SELECT_PROVIDER_BY_ID, {"provider_id": provider_id}).mappings().first()
        if row is None:
            return None
        return _row_to_dict(row, _PROVIDER_COLS)


def get_provider_by_code(provider_code: str) -> dict | None:
    """Return a provider row by unique code, or None."""
    with SessionLocal() as db:
        row = db.execute(_SELECT_PROVIDER_BY_CODE, {"provider_code": provider_code}).mappings().first()
        if row is None:
            return None
        return _row_to_dict(row, _PROVIDER_COLS)


def get_providers(
    *,
    page: int = 1,
    page_size: int = 50,
    provider_type: str | None = None,
    status: str | None = None,
    search: str | None = None,
) -> tuple[list[dict], int]:
    """Return paginated provider rows."""
    params = {
        "provider_type": provider_type,
        "status": status,
        "search": search,
        "limit": page_size,
        "offset": (page - 1) * page_size,
    }
    with SessionLocal() as db:
        total = db.execute(_COUNT_PROVIDERS, params).scalar_one()
        rows = db.execute(_QUERY_PROVIDERS, params).mappings().all()
        items = [_row_to_dict(row, _PROVIDER_COLS) for row in rows]
        return items, total


def update_provider(
    provider_id: UUID,
    *,
    name: str | None = None,
    provider_type: str | None = None,
    status: str | None = None,
    endpoint_base: str | None = None,
    config_json: dict | None = None,
) -> bool:
    """Update an existing provider. Returns True if a row was updated."""
    with SessionLocal() as db:
        result = db.execute(
            _UPDATE_PROVIDER,
            {
                "provider_id": provider_id,
                "name": name,
                "provider_type": provider_type,
                "status": status,
                "endpoint_base": endpoint_base,
                "config_json": json.dumps(config_json) if config_json is not None else None,
            },
        )
        db.commit()
        return result.rowcount > 0


# ═══════════════════════════════════════════════════════════════════════════════
# Provider Models
# ═══════════════════════════════════════════════════════════════════════════════

_MODEL_COLS = [
    "provider_model_id", "provider_id", "model_code", "external_model_id",
    "model_type", "status", "display_name", "version_label",
    "context_window_tokens", "max_input_tokens", "max_output_tokens",
    "input_price_per_1k", "output_price_per_1k", "currency_code",
    "supports_streaming", "supports_json_mode", "supports_tools", "supports_vision",
    "sensitivity_ceiling", "config_json", "metadata_json",
    "deprecated_at", "created_at", "updated_at",
]

_INSERT_MODEL = text("""
    INSERT INTO provider_models (
        provider_model_id, provider_id, model_code, external_model_id,
        model_type, status,
        display_name, version_label,
        context_window_tokens, max_input_tokens, max_output_tokens,
        input_price_per_1k, output_price_per_1k, currency_code,
        supports_streaming, supports_json_mode, supports_tools, supports_vision,
        sensitivity_ceiling, config_json, metadata_json, deprecated_at
    ) VALUES (
        :provider_model_id, :provider_id, :model_code, :external_model_id,
        :model_type, :status,
        :display_name, :version_label,
        :context_window_tokens, :max_input_tokens, :max_output_tokens,
        :input_price_per_1k, :output_price_per_1k, :currency_code,
        :supports_streaming, :supports_json_mode, :supports_tools, :supports_vision,
        :sensitivity_ceiling, cast(:config_json AS JSONB), cast(:metadata_json AS JSONB), :deprecated_at
    )
    RETURNING provider_model_id
""")

_SELECT_MODEL_BY_ID = text("""
    SELECT provider_model_id, provider_id, model_code, external_model_id,
           model_type, status, display_name, version_label,
           context_window_tokens, max_input_tokens, max_output_tokens,
           input_price_per_1k, output_price_per_1k, currency_code,
           supports_streaming, supports_json_mode, supports_tools, supports_vision,
           sensitivity_ceiling, config_json, metadata_json,
           deprecated_at, created_at, updated_at
    FROM provider_models
    WHERE provider_model_id = :provider_model_id
""").bindparams(bindparam("provider_model_id", ))

_COUNT_MODELS = text("""
    SELECT count(*) FROM provider_models
    WHERE provider_id = :provider_id
      AND (:model_type IS NULL OR model_type = :model_type)
      AND (:status IS NULL OR status = :status)
      AND (:search IS NULL
           OR model_code LIKE '%' || :search || '%'
           OR external_model_id LIKE '%' || :search || '%'
           OR display_name LIKE '%' || :search || '%')
""").bindparams(bindparam("provider_id", ))

_QUERY_MODELS = text("""
    SELECT provider_model_id, provider_id, model_code, external_model_id,
           model_type, status, display_name, version_label,
           context_window_tokens, max_input_tokens, max_output_tokens,
           input_price_per_1k, output_price_per_1k, currency_code,
           supports_streaming, supports_json_mode, supports_tools, supports_vision,
           sensitivity_ceiling, config_json, metadata_json,
           deprecated_at, created_at, updated_at
    FROM provider_models
    WHERE provider_id = :provider_id
      AND (:model_type IS NULL OR model_type = :model_type)
      AND (:status IS NULL OR status = :status)
      AND (:search IS NULL
           OR model_code LIKE '%' || :search || '%'
           OR external_model_id LIKE '%' || :search || '%'
           OR display_name LIKE '%' || :search || '%')
    ORDER BY created_at DESC
    LIMIT :limit OFFSET :offset
""").bindparams(bindparam("provider_id", ))

_UPDATE_MODEL = text("""
    UPDATE provider_models
    SET status               = COALESCE(:status, status),
        display_name         = COALESCE(:display_name, display_name),
        version_label        = COALESCE(:version_label, version_label),
        context_window_tokens = COALESCE(:context_window_tokens, context_window_tokens),
        max_input_tokens     = COALESCE(:max_input_tokens, max_input_tokens),
        max_output_tokens    = COALESCE(:max_output_tokens, max_output_tokens),
        input_price_per_1k   = COALESCE(:input_price_per_1k, input_price_per_1k),
        output_price_per_1k  = COALESCE(:output_price_per_1k, output_price_per_1k),
        supports_streaming   = COALESCE(:supports_streaming, supports_streaming),
        supports_json_mode   = COALESCE(:supports_json_mode, supports_json_mode),
        supports_tools       = COALESCE(:supports_tools, supports_tools),
        supports_vision      = COALESCE(:supports_vision, supports_vision),
        sensitivity_ceiling  = COALESCE(:sensitivity_ceiling, sensitivity_ceiling),
        config_json          = COALESCE(cast(:config_json AS JSONB), config_json),
        metadata_json        = COALESCE(cast(:metadata_json AS JSONB), metadata_json),
        deprecated_at        = COALESCE(:deprecated_at, deprecated_at),
        updated_at           = CURRENT_TIMESTAMP
    WHERE provider_model_id = :provider_model_id
    RETURNING provider_model_id
""").bindparams(
    bindparam("provider_model_id", ),
)


def create_provider_model(
    *,
    provider_id: UUID,
    model_code: str,
    external_model_id: str,
    model_type: str,
    status: str = "active",
    display_name: str | None = None,
    version_label: str | None = None,
    context_window_tokens: int | None = None,
    max_input_tokens: int | None = None,
    max_output_tokens: int | None = None,
    input_price_per_1k: float | None = None,
    output_price_per_1k: float | None = None,
    currency_code: str = "USD",
    supports_streaming: bool = False,
    supports_json_mode: bool = False,
    supports_tools: bool = False,
    supports_vision: bool = False,
    sensitivity_ceiling: str = "private",
    config_json: dict | None = None,
    metadata_json: dict | None = None,
    deprecated_at: str | None = None,
) -> dict:
    """Insert a new provider_model. Returns the full row as a dict."""
    provider_model_id = uuid4()
    with SessionLocal() as db:
        new_id = db.execute(
            _INSERT_MODEL,
            {
                "provider_model_id": provider_model_id,
                "provider_id": provider_id,
                "model_code": model_code,
                "external_model_id": external_model_id,
                "model_type": model_type,
                "status": status,
                "display_name": display_name,
                "version_label": version_label,
                "context_window_tokens": context_window_tokens,
                "max_input_tokens": max_input_tokens,
                "max_output_tokens": max_output_tokens,
                "input_price_per_1k": input_price_per_1k,
                "output_price_per_1k": output_price_per_1k,
                "currency_code": currency_code,
                "supports_streaming": supports_streaming,
                "supports_json_mode": supports_json_mode,
                "supports_tools": supports_tools,
                "supports_vision": supports_vision,
                "sensitivity_ceiling": sensitivity_ceiling,
                "config_json": json.dumps(config_json or {}),
                "metadata_json": json.dumps(metadata_json or {}),
                "deprecated_at": deprecated_at,
            },
        ).scalar_one()
        db.commit()

    return get_provider_model_by_id(new_id)


def get_provider_model_by_id(provider_model_id: UUID) -> dict | None:
    """Return a provider_model row by primary key, or None."""
    with SessionLocal() as db:
        row = db.execute(_SELECT_MODEL_BY_ID, {"provider_model_id": provider_model_id}).mappings().first()
        if row is None:
            return None
        return _row_to_dict(row, _MODEL_COLS)


def get_provider_models(
    *,
    provider_id: UUID,
    page: int = 1,
    page_size: int = 50,
    model_type: str | None = None,
    status: str | None = None,
    search: str | None = None,
) -> tuple[list[dict], int]:
    """Return paginated provider_model rows for a given provider."""
    params = {
        "provider_id": provider_id,
        "model_type": model_type,
        "status": status,
        "search": search,
        "limit": page_size,
        "offset": (page - 1) * page_size,
    }
    with SessionLocal() as db:
        total = db.execute(_COUNT_MODELS, params).scalar_one()
        rows = db.execute(_QUERY_MODELS, params).mappings().all()
        items = [_row_to_dict(row, _MODEL_COLS) for row in rows]
        return items, total


def update_provider_model(
    provider_model_id: UUID,
    *,
    status: str | None = None,
    display_name: str | None = None,
    version_label: str | None = None,
    context_window_tokens: int | None = None,
    max_input_tokens: int | None = None,
    max_output_tokens: int | None = None,
    input_price_per_1k: float | None = None,
    output_price_per_1k: float | None = None,
    supports_streaming: bool | None = None,
    supports_json_mode: bool | None = None,
    supports_tools: bool | None = None,
    supports_vision: bool | None = None,
    sensitivity_ceiling: str | None = None,
    config_json: dict | None = None,
    metadata_json: dict | None = None,
    deprecated_at: str | None = None,
) -> bool:
    """Update an existing provider_model. Returns True if a row was updated."""
    with SessionLocal() as db:
        result = db.execute(
            _UPDATE_MODEL,
            {
                "provider_model_id": provider_model_id,
                "status": status,
                "display_name": display_name,
                "version_label": version_label,
                "context_window_tokens": context_window_tokens,
                "max_input_tokens": max_input_tokens,
                "max_output_tokens": max_output_tokens,
                "input_price_per_1k": input_price_per_1k,
                "output_price_per_1k": output_price_per_1k,
                "supports_streaming": supports_streaming,
                "supports_json_mode": supports_json_mode,
                "supports_tools": supports_tools,
                "supports_vision": supports_vision,
                "sensitivity_ceiling": sensitivity_ceiling,
                "config_json": json.dumps(config_json) if config_json is not None else None,
                "metadata_json": json.dumps(metadata_json) if metadata_json is not None else None,
                "deprecated_at": deprecated_at,
            },
        )
        db.commit()
        return result.rowcount > 0


# ═══════════════════════════════════════════════════════════════════════════════
# Capabilities
# ═══════════════════════════════════════════════════════════════════════════════

_CAPABILITY_COLS = [
    "capability_id", "capability_code", "name", "category",
    "risk_level", "default_budget_mode", "created_at", "updated_at",
]

_INSERT_CAPABILITY = text("""
    INSERT INTO capabilities (capability_id, capability_code, name, category, risk_level, default_budget_mode)
    VALUES (:capability_id, :capability_code, :name, :category, :risk_level, :default_budget_mode)
    RETURNING capability_id
""")

_SELECT_CAPABILITY_BY_ID = text("""
    SELECT capability_id, capability_code, name, category,
           risk_level, default_budget_mode, created_at, updated_at
    FROM capabilities
    WHERE capability_id = :capability_id
""").bindparams(bindparam("capability_id", ))

_SELECT_CAPABILITY_BY_CODE = text("""
    SELECT capability_id, capability_code, name, category,
           risk_level, default_budget_mode, created_at, updated_at
    FROM capabilities
    WHERE capability_code = :capability_code
    LIMIT 1
""")

_COUNT_CAPABILITIES = text("""
    SELECT count(*) FROM capabilities
    WHERE 1=1
      AND (:category IS NULL OR category = :category)
      AND (:risk_level IS NULL OR risk_level = :risk_level)
      AND (:search IS NULL
           OR capability_code LIKE '%' || :search || '%'
           OR name LIKE '%' || :search || '%')
""")

_QUERY_CAPABILITIES = text("""
    SELECT capability_id, capability_code, name, category,
           risk_level, default_budget_mode, created_at, updated_at
    FROM capabilities
    WHERE 1=1
      AND (:category IS NULL OR category = :category)
      AND (:risk_level IS NULL OR risk_level = :risk_level)
      AND (:search IS NULL
           OR capability_code LIKE '%' || :search || '%'
           OR name LIKE '%' || :search || '%')
    ORDER BY category, capability_code
    LIMIT :limit OFFSET :offset
""")

_UPDATE_CAPABILITY = text("""
    UPDATE capabilities
    SET name                = COALESCE(:name, name),
        category            = COALESCE(:category, category),
        risk_level          = COALESCE(:risk_level, risk_level),
        default_budget_mode = COALESCE(:default_budget_mode, default_budget_mode),
        updated_at          = CURRENT_TIMESTAMP
    WHERE capability_id = :capability_id
    RETURNING capability_id
""").bindparams(bindparam("capability_id", ))

_UPSERT_CAPABILITY = text("""
    INSERT INTO capabilities (capability_id, capability_code, name, category, risk_level, default_budget_mode)
    VALUES (:capability_id, :capability_code, :name, :category, :risk_level, :default_budget_mode)
    ON CONFLICT (capability_code) DO UPDATE SET
        name                = EXCLUDED.name,
        category            = EXCLUDED.category,
        risk_level          = EXCLUDED.risk_level,
        default_budget_mode = EXCLUDED.default_budget_mode,
        updated_at          = CURRENT_TIMESTAMP
    RETURNING capability_id
""")


def create_capability(
    *,
    capability_code: str,
    name: str,
    category: str,
    risk_level: str = "normal",
    default_budget_mode: str = "metered",
) -> dict:
    """Insert a new capability. Returns the full row as a dict."""
    capability_id = uuid4()
    with SessionLocal() as db:
        new_id = db.execute(
            _INSERT_CAPABILITY,
            {
                "capability_id": capability_id,
                "capability_code": capability_code,
                "name": name,
                "category": category,
                "risk_level": risk_level,
                "default_budget_mode": default_budget_mode,
            },
        ).scalar_one()
        db.commit()

    return get_capability_by_id(new_id)


def get_capability_by_id(capability_id: UUID) -> dict | None:
    """Return a capability row by primary key, or None."""
    with SessionLocal() as db:
        row = db.execute(_SELECT_CAPABILITY_BY_ID, {"capability_id": capability_id}).mappings().first()
        if row is None:
            return None
        return _row_to_dict(row, _CAPABILITY_COLS)


def get_capability_by_code(capability_code: str) -> dict | None:
    """Return a capability row by unique code, or None."""
    with SessionLocal() as db:
        row = db.execute(_SELECT_CAPABILITY_BY_CODE, {"capability_code": capability_code}).mappings().first()
        if row is None:
            return None
        return _row_to_dict(row, _CAPABILITY_COLS)


def get_capabilities(
    *,
    page: int = 1,
    page_size: int = 50,
    category: str | None = None,
    risk_level: str | None = None,
    search: str | None = None,
) -> tuple[list[dict], int]:
    """Return paginated capability rows."""
    params = {
        "category": category,
        "risk_level": risk_level,
        "search": search,
        "limit": page_size,
        "offset": (page - 1) * page_size,
    }
    with SessionLocal() as db:
        total = db.execute(_COUNT_CAPABILITIES, params).scalar_one()
        rows = db.execute(_QUERY_CAPABILITIES, params).mappings().all()
        items = [_row_to_dict(row, _CAPABILITY_COLS) for row in rows]
        return items, total


def update_capability(
    capability_id: UUID,
    *,
    name: str | None = None,
    category: str | None = None,
    risk_level: str | None = None,
    default_budget_mode: str | None = None,
) -> bool:
    """Update an existing capability. Returns True if a row was updated."""
    with SessionLocal() as db:
        result = db.execute(
            _UPDATE_CAPABILITY,
            {
                "capability_id": capability_id,
                "name": name,
                "category": category,
                "risk_level": risk_level,
                "default_budget_mode": default_budget_mode,
            },
        )
        db.commit()
        return result.rowcount > 0


def seed_capabilities(seed_data: list[dict[str, str]]) -> list[dict]:
    """Upsert pre-defined capabilities. Returns the inserted/updated rows."""
    results: list[dict] = []
    with SessionLocal() as db:
        for cap in seed_data:
            cap_id = uuid4()
            new_id = db.execute(
                _UPSERT_CAPABILITY,
                {
                    "capability_id": cap_id,
                    "capability_code": cap["capability_code"],
                    "name": cap["name"],
                    "category": cap["category"],
                    "risk_level": cap["risk_level"],
                    "default_budget_mode": cap["default_budget_mode"],
                },
            ).scalar_one()
            results.append({"capability_id": str(new_id), **cap})
        db.commit()
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# Capability Bindings
# ═══════════════════════════════════════════════════════════════════════════════

_BINDING_COLS = [
    "capability_binding_id", "capability_id", "provider_id", "provider_model_id",
    "credential_id", "project_id", "binding_scope", "status", "priority",
    "sensitivity_floor", "sensitivity_ceiling", "budget_mode",
    "require_review", "allow_streaming", "timeout_seconds", "rate_limit_key",
    "policy_json", "metadata_json", "created_by_user_id",
    "created_at", "updated_at",
]

_INSERT_BINDING = text("""
    INSERT INTO capability_bindings (
        capability_binding_id, capability_id, provider_id, provider_model_id,
        credential_id, project_id,
        binding_scope, status, priority,
        sensitivity_floor, sensitivity_ceiling, budget_mode,
        require_review, allow_streaming, timeout_seconds, rate_limit_key,
        policy_json, metadata_json, created_by_user_id
    ) VALUES (
        :capability_binding_id, :capability_id, :provider_id, :provider_model_id,
        :credential_id, :project_id,
        :binding_scope, :status, :priority,
        :sensitivity_floor, :sensitivity_ceiling, :budget_mode,
        :require_review, :allow_streaming, :timeout_seconds, :rate_limit_key,
        cast(:policy_json AS JSONB), cast(:metadata_json AS JSONB), :created_by_user_id
    )
    RETURNING capability_binding_id
""")

_SELECT_BINDING_BY_ID = text("""
    SELECT capability_binding_id, capability_id, provider_id, provider_model_id,
           credential_id, project_id, binding_scope, status, priority,
           sensitivity_floor, sensitivity_ceiling, budget_mode,
           require_review, allow_streaming, timeout_seconds, rate_limit_key,
           policy_json, metadata_json, created_by_user_id,
           created_at, updated_at
    FROM capability_bindings
    WHERE capability_binding_id = :capability_binding_id
""").bindparams(bindparam("capability_binding_id", ))

_COUNT_BINDINGS = text("""
    SELECT count(*) FROM capability_bindings
    WHERE 1=1
      AND (:capability_id IS NULL OR capability_id = :capability_id)
      AND (:provider_id IS NULL OR provider_id = :provider_id)
      AND (:project_id IS NULL OR project_id = :project_id)
      AND (:status IS NULL OR status = :status)
      AND (:binding_scope IS NULL OR binding_scope = :binding_scope)
""")

_QUERY_BINDINGS = text("""
    SELECT capability_binding_id, capability_id, provider_id, provider_model_id,
           credential_id, project_id, binding_scope, status, priority,
           sensitivity_floor, sensitivity_ceiling, budget_mode,
           require_review, allow_streaming, timeout_seconds, rate_limit_key,
           policy_json, metadata_json, created_by_user_id,
           created_at, updated_at
    FROM capability_bindings
    WHERE 1=1
      AND (:capability_id IS NULL OR capability_id = :capability_id)
      AND (:provider_id IS NULL OR provider_id = :provider_id)
      AND (:project_id IS NULL OR project_id = :project_id)
      AND (:status IS NULL OR status = :status)
      AND (:binding_scope IS NULL OR binding_scope = :binding_scope)
    ORDER BY priority ASC, created_at DESC
    LIMIT :limit OFFSET :offset
""")

_UPDATE_BINDING = text("""
    UPDATE capability_bindings
    SET provider_model_id  = COALESCE(:provider_model_id, provider_model_id),
        credential_id      = COALESCE(:credential_id, credential_id),
        status             = COALESCE(:status, status),
        priority           = COALESCE(:priority, priority),
        sensitivity_floor  = COALESCE(:sensitivity_floor, sensitivity_floor),
        sensitivity_ceiling= COALESCE(:sensitivity_ceiling, sensitivity_ceiling),
        budget_mode        = COALESCE(:budget_mode, budget_mode),
        require_review     = COALESCE(:require_review, require_review),
        allow_streaming    = COALESCE(:allow_streaming, allow_streaming),
        timeout_seconds    = COALESCE(:timeout_seconds, timeout_seconds),
        rate_limit_key     = COALESCE(:rate_limit_key, rate_limit_key),
        policy_json        = COALESCE(cast(:policy_json AS JSONB), policy_json),
        metadata_json      = COALESCE(cast(:metadata_json AS JSONB), metadata_json),
        updated_at         = CURRENT_TIMESTAMP
    WHERE capability_binding_id = :capability_binding_id
    RETURNING capability_binding_id
""").bindparams(
    bindparam("capability_binding_id", ),
    bindparam("provider_model_id", ),
    bindparam("credential_id", ),
)


def create_capability_binding(
    *,
    capability_id: UUID,
    provider_id: UUID,
    provider_model_id: UUID | None = None,
    credential_id: UUID | None = None,
    project_id: UUID | None = None,
    binding_scope: str = "global",
    status: str = "active",
    priority: int = 100,
    sensitivity_floor: str = "public",
    sensitivity_ceiling: str = "private",
    budget_mode: str = "metered",
    require_review: bool = False,
    allow_streaming: bool = True,
    timeout_seconds: int = 120,
    rate_limit_key: str | None = None,
    policy_json: dict | None = None,
    metadata_json: dict | None = None,
    created_by_user_id: UUID | None = None,
) -> dict:
    """Insert a new capability binding. Returns the full row as a dict."""
    capability_binding_id = uuid4()
    with SessionLocal() as db:
        new_id = db.execute(
            _INSERT_BINDING,
            {
                "capability_binding_id": capability_binding_id,
                "capability_id": capability_id,
                "provider_id": provider_id,
                "provider_model_id": provider_model_id,
                "credential_id": credential_id,
                "project_id": project_id,
                "binding_scope": binding_scope,
                "status": status,
                "priority": priority,
                "sensitivity_floor": sensitivity_floor,
                "sensitivity_ceiling": sensitivity_ceiling,
                "budget_mode": budget_mode,
                "require_review": require_review,
                "allow_streaming": allow_streaming,
                "timeout_seconds": timeout_seconds,
                "rate_limit_key": rate_limit_key,
                "policy_json": json.dumps(policy_json or {}),
                "metadata_json": json.dumps(metadata_json or {}),
                "created_by_user_id": created_by_user_id,
            },
        ).scalar_one()
        db.commit()

    return get_capability_binding_by_id(new_id)


def get_capability_binding_by_id(capability_binding_id: UUID) -> dict | None:
    """Return a capability_binding row by primary key, or None."""
    with SessionLocal() as db:
        row = db.execute(_SELECT_BINDING_BY_ID, {"capability_binding_id": capability_binding_id}).mappings().first()
        if row is None:
            return None
        return _row_to_dict(row, _BINDING_COLS)


def get_capability_bindings(
    *,
    page: int = 1,
    page_size: int = 50,
    capability_id: UUID | None = None,
    provider_id: UUID | None = None,
    project_id: UUID | None = None,
    status: str | None = None,
    binding_scope: str | None = None,
) -> tuple[list[dict], int]:
    """Return paginated capability binding rows."""
    params = {
        "capability_id": capability_id,
        "provider_id": provider_id,
        "project_id": project_id,
        "status": status,
        "binding_scope": binding_scope,
        "limit": page_size,
        "offset": (page - 1) * page_size,
    }
    with SessionLocal() as db:
        total = db.execute(_COUNT_BINDINGS, params).scalar_one()
        rows = db.execute(_QUERY_BINDINGS, params).mappings().all()
        items = [_row_to_dict(row, _BINDING_COLS) for row in rows]
        return items, total


# ═══════════════════════════════════════════════════════════════════════════════
# Binding Resolution (P2-12 — Gateway routing)
# ═══════════════════════════════════════════════════════════════════════════════

_RESOLVE_BINDING = text("""
    SELECT cb.capability_binding_id, cb.capability_id, cb.provider_id,
           cb.provider_model_id, cb.credential_id, cb.project_id,
           cb.binding_scope, cb.status, cb.priority,
           cb.sensitivity_floor, cb.sensitivity_ceiling, cb.budget_mode,
           cb.require_review, cb.allow_streaming, cb.timeout_seconds,
           cb.rate_limit_key, cb.policy_json, cb.metadata_json,
           cb.created_by_user_id, cb.created_at, cb.updated_at,
           p.provider_code, p.name AS provider_name,
           p.provider_type, p.status AS provider_status,
           p.endpoint_base,
           pm.model_code, pm.external_model_id, pm.model_type,
           pm.status AS model_status, pm.display_name,
           pm.context_window_tokens, pm.max_input_tokens, pm.max_output_tokens,
           pm.input_price_per_1k, pm.output_price_per_1k, pm.currency_code,
           pm.supports_streaming, pm.supports_json_mode,
           pm.supports_tools, pm.supports_vision,
           pm.sensitivity_ceiling AS model_sensitivity_ceiling,
           pm.config_json AS model_config_json,
           c.capability_code, c.name AS capability_name,
           c.category AS capability_category,
           c.risk_level AS capability_risk_level
    FROM capability_bindings cb
    JOIN capabilities c ON c.capability_id = cb.capability_id
    JOIN providers p ON p.provider_id = cb.provider_id
    LEFT JOIN provider_models pm ON pm.provider_model_id = cb.provider_model_id
    WHERE c.capability_code = :capability_code
      AND cb.status = 'active'
      AND p.status != 'disabled'
      AND (:project_id IS NULL
           OR cb.project_id IS NULL
           OR cb.project_id = :project_id
           OR cb.binding_scope = 'global')
      AND (:sensitivity IS NULL
           OR cb.sensitivity_floor <= :sensitivity
           AND cb.sensitivity_ceiling >= :sensitivity)
    ORDER BY
        CASE WHEN cb.project_id = :project_id THEN 0 ELSE 1 END,
        cb.priority ASC,
        cb.created_at DESC
    LIMIT 1
""").bindparams(
    bindparam("project_id", ),
)

_RESOLVE_BINDING_COLS = [
    "capability_binding_id", "capability_id", "provider_id",
    "provider_model_id", "credential_id", "project_id",
    "binding_scope", "status", "priority",
    "sensitivity_floor", "sensitivity_ceiling", "budget_mode",
    "require_review", "allow_streaming", "timeout_seconds",
    "rate_limit_key", "policy_json", "metadata_json",
    "created_by_user_id", "created_at", "updated_at",
    "provider_code", "provider_name", "provider_type", "provider_status",
    "endpoint_base", "model_code", "external_model_id", "model_type",
    "model_status", "display_name",
    "context_window_tokens", "max_input_tokens", "max_output_tokens",
    "input_price_per_1k", "output_price_per_1k", "currency_code",
    "supports_streaming", "supports_json_mode",
    "supports_tools", "supports_vision",
    "model_sensitivity_ceiling", "model_config_json",
    "capability_code", "capability_name",
    "capability_category", "capability_risk_level",
]


def resolve_capability_binding(
    *,
    capability_code: str,
    project_id: UUID | None = None,
    sensitivity: str | None = None,
) -> dict | None:
    """Resolve the best-matching active capability binding for a Gateway call.

    Selection rules:
    1. Exact project match or global-scope binding.
    2. Sensitivity must be within the binding's floor/ceiling range.
    3. Binding must be active, provider must not be disabled.
    4. Prefer project-scoped bindings over global, then lowest priority number.

    Returns a combined dict with binding + provider + model + capability fields,
    or None if no suitable binding exists.
    """
    with SessionLocal() as db:
        row = db.execute(
            _RESOLVE_BINDING,
            {
                "capability_code": capability_code,
                "project_id": project_id,
                "sensitivity": sensitivity,
            },
        ).mappings().first()
        if row is None:
            return None
        result = _row_to_dict(row, _RESOLVE_BINDING_COLS)
        # Rename provider-level fields for clarity
        result["provider_code_val"] = result.pop("provider_code", None)
        result["provider_status_val"] = result.pop("provider_status", None)
        return result


def update_capability_binding(
    capability_binding_id: UUID,
    *,
    provider_model_id: UUID | None = None,
    credential_id: UUID | None = None,
    status: str | None = None,
    priority: int | None = None,
    sensitivity_floor: str | None = None,
    sensitivity_ceiling: str | None = None,
    budget_mode: str | None = None,
    require_review: bool | None = None,
    allow_streaming: bool | None = None,
    timeout_seconds: int | None = None,
    rate_limit_key: str | None = None,
    policy_json: dict | None = None,
    metadata_json: dict | None = None,
) -> bool:
    """Update an existing capability binding. Returns True if a row was updated."""
    with SessionLocal() as db:
        result = db.execute(
            _UPDATE_BINDING,
            {
                "capability_binding_id": capability_binding_id,
                "provider_model_id": provider_model_id,
                "credential_id": credential_id,
                "status": status,
                "priority": priority,
                "sensitivity_floor": sensitivity_floor,
                "sensitivity_ceiling": sensitivity_ceiling,
                "budget_mode": budget_mode,
                "require_review": require_review,
                "allow_streaming": allow_streaming,
                "timeout_seconds": timeout_seconds,
                "rate_limit_key": rate_limit_key,
                "policy_json": json.dumps(policy_json) if policy_json is not None else None,
                "metadata_json": json.dumps(metadata_json) if metadata_json is not None else None,
            },
        )
        db.commit()
        return result.rowcount > 0
