"""Migration manifest — constants, table/column mappings, and configuration.

Defines the canonical mapping from Mneme-legacy (SQLite) schema to
Mneme-v4 (PostgreSQL) schema.  Every table migration uses these
definitions to plan, dump, map, load, and verify.

Design
------
* ``TABLE_MAP`` — legacy table name → v4 table name (None = skip)
* ``COLUMN_MAP`` — per-table dict of legacy_column → (v4_column, transform_name)
* ``ENUM_MAP`` — legacy enum string → v4 enum string
* ``MIGRATION_VERSION`` — schema version tag carried in migration reports
"""

from __future__ import annotations

import uuid
from typing import Any

# ═══════════════════════════════════════════════════════════════════════════════
# Version
# ═══════════════════════════════════════════════════════════════════════════════

MIGRATION_VERSION: str = "v1.0.0"

# ═══════════════════════════════════════════════════════════════════════════════
# Table mapping: legacy SQLite name → v4 PostgreSQL name (None = skip)
# ═══════════════════════════════════════════════════════════════════════════════

TABLE_MAP: dict[str, str | None] = {
    # Core entity tables
    "users": "auth_users",
    "sessions": "auth_sessions",
    "projects": "projects",
    "agents": "agents",
    "agent_tokens": "agent_tokens",
    "memories": "memories",
    "memory_candidates": "memory_candidates",
    "memory_index_entries": "memory_index_entries",
    "memory_relations": "memory_relations",
    "conversations": "conversations",
    "messages": "messages",
    "raw_events": "raw_events",
    "event_sources": "event_sources",
    "context_packs": "context_packs",
    "context_pack_items": "context_pack_items",
    # Gateways & pipelines
    "gateway_providers": "gateway_providers",
    "gateway_capabilities": "gateway_capabilities",
    "gateway_bindings": "gateway_capability_bindings",
    "gateway_limits": "gateway_limits",
    "pipeline_defs": "pipeline_defs",
    "pipeline_runs": "pipeline_runs",
    # Review & audit
    "review_items": "review_items",
    "review_policies": "review_policies",
    "audit_events": "audit_events",
    "dead_letters": "dead_letters",
    # Knowledge & storage
    "knowledge_documents": "knowledge_documents",
    "knowledge_blocks": "knowledge_blocks",
    "knowledge_chunks": "knowledge_chunks",
    "assets": "assets",
    "asset_metadata": "asset_metadata",
    "inbox_items": "inbox_items",
    # Vault
    "vault_credentials": "vault_credentials",
    "vault_access_logs": "vault_access_logs",
    # Admin
    "admin_events": "admin_events",
    # Legacy tables not present in v4 — skip
    "item_tags": None,
    "item_attachments": None,
    "system_config": None,
}

# ═══════════════════════════════════════════════════════════════════════════════
# Column mapping: legacy_col → (v4_col, transform | None)
# ═══════════════════════════════════════════════════════════════════════════════

COLUMN_MAP: dict[str, dict[str, tuple[str, str | None]]] = {
    "memories": {
        "id": ("id", "uuid"),
        "project_id": ("project_id", "uuid"),
        "agent_id": ("agent_id", "uuid"),
        "title": ("title", None),
        "memory_text": ("memory_text", None),
        "status": ("status", "enum:memory_status"),
        "version": ("version", "int"),
        "sensitivity": ("sensitivity", "enum:sensitivity"),
        "tags": ("tags", "json"),
        "metadata": ("metadata", "json"),
        "created_at": ("created_at", "datetime"),
        "updated_at": ("updated_at", "datetime"),
        "expired_at": ("expired_at", "datetime"),
        "deleted_at": ("deleted_at", "datetime"),
        "source_id": ("source_id", "skip"),
        "source_type": ("source_type", "skip"),
    },
    "agents": {
        "id": ("id", "uuid"),
        "project_id": ("project_id", "uuid"),
        "name": ("name", None),
        "description": ("description", None),
        "status": ("status", "enum:agent_status"),
        "metadata": ("metadata", "json"),
        "created_at": ("created_at", "datetime"),
        "updated_at": ("updated_at", "datetime"),
    },
    "conversations": {
        "id": ("id", "uuid"),
        "project_id": ("project_id", "uuid"),
        "agent_id": ("agent_id", "uuid"),
        "title": ("title", None),
        "status": ("status", "enum:conversation_status"),
        "conv_type": ("conv_type", "enum:conversation_type"),
        "metadata": ("metadata", "json"),
        "created_at": ("created_at", "datetime"),
        "updated_at": ("updated_at", "datetime"),
    },
    "messages": {
        "id": ("id", "uuid"),
        "conversation_id": ("conversation_id", "uuid"),
        "role": ("role", "enum:role_code"),
        "content": ("content", None),
        "model": ("model", None),
        "token_count": ("token_count", "int"),
        "metadata": ("metadata", "json"),
        "created_at": ("created_at", "datetime"),
    },
    "projects": {
        "id": ("id", "uuid"),
        "name": ("name", None),
        "description": ("description", None),
        "status": ("status", "enum:project_status"),
        "metadata": ("metadata", "json"),
        "created_at": ("created_at", "datetime"),
    },
    "gateway_providers": {
        "id": ("id", "uuid"),
        "name": ("name", None),
        "provider_type": ("provider_type", "enum:provider_type"),
        "status": ("status", "enum:provider_status"),
        "config": ("config", "json"),
        "created_at": ("created_at", "datetime"),
    },
    "pipeline_runs": {
        "id": ("id", "uuid"),
        "pipeline_def_id": ("pipeline_def_id", "uuid"),
        "status": ("status", "enum:pipeline_run_status"),
        "trigger_type": ("trigger_type", "enum:pipeline_trigger_type"),
        "input_payload": ("input_payload", "json"),
        "output_payload": ("output_payload", "json"),
        "error_message": ("error_message", None),
        "started_at": ("started_at", "datetime"),
        "finished_at": ("finished_at", "datetime"),
        "created_at": ("created_at", "datetime"),
    },
    "review_items": {
        "id": ("id", "uuid"),
        "project_id": ("project_id", "uuid"),
        "review_type": ("review_type", "enum:review_type"),
        "target_type": ("target_type", "enum:review_target_type"),
        "target_id": ("target_id", "uuid"),
        "status": ("status", "enum:review_status"),
        "decision": ("decision", "enum:review_decision"),
        "reason": ("reason", None),
        "reviewer_id": ("reviewer_id", "uuid"),
        "created_at": ("created_at", "datetime"),
        "reviewed_at": ("reviewed_at", "datetime"),
    },
}

# ═══════════════════════════════════════════════════════════════════════════════
# Enum value mappings: legacy value → v4 value
# ═══════════════════════════════════════════════════════════════════════════════

ENUM_MAP: dict[str, dict[str, str]] = {
    "memory_status": {
        "draft": "draft",
        "active": "active",
        "expired": "expired",
        "deleted": "deleted",
        "archived": "expired",
    },
    "agent_status": {
        "active": "active",
        "inactive": "disabled",
        "suspended": "disabled",
    },
    "conversation_status": {
        "active": "active",
        "archived": "archived",
        "deleted": "deleted",
    },
    "conversation_type": {
        "chat": "chat",
        "workflow": "workflow",
        "support": "support",
    },
    "project_status": {
        "active": "active",
        "archived": "archived",
    },
    "sensitivity": {
        "public": "public",
        "normal": "normal",
        "private": "private",
        "sensitive": "sensitive",
        "secret": "secret",
    },
    "role_code": {
        "user": "user",
        "assistant": "assistant",
        "system": "system",
        "tool": "tool",
        "function": "tool",
    },
    "provider_type": {
        "openai": "openai",
        "anthropic": "anthropic",
        "local": "local",
        "custom": "custom",
    },
    "provider_status": {
        "active": "active",
        "disabled": "disabled",
    },
    "pipeline_run_status": {
        "pending": "pending",
        "running": "running",
        "succeeded": "succeeded",
        "failed": "failed",
        "cancelled": "cancelled",
    },
    "pipeline_trigger_type": {
        "manual": "manual",
        "event": "event",
        "schedule": "schedule",
        "importer": "importer",
    },
    "review_type": {
        "auto": "auto",
        "manual": "manual",
    },
    "review_target_type": {
        "memory": "memory",
        "memory_candidate": "memory_candidate",
        "event": "event",
    },
    "review_status": {
        "pending": "pending",
        "approved": "approved",
        "rejected": "rejected",
        "cancelled": "cancelled",
    },
    "review_decision": {
        "approve": "approve",
        "reject": "reject",
    },
}

# ═══════════════════════════════════════════════════════════════════════════════
# Default values for new v4 columns not present in legacy
# ═══════════════════════════════════════════════════════════════════════════════

NEW_COLUMN_DEFAULTS: dict[str, dict[str, Any]] = {
    "memories": {
        "embedding_model": None,
        "fts_state": "ready",
        "vector_state": "pending",
    },
    "agents": {
        "agent_type": "default",
    },
    "gateway_bindings": {
        "budget_mode": "unlimited",
        "binding_scope": "project",
    },
}

# ═══════════════════════════════════════════════════════════════════════════════
# Migration order — tables in dependency order (parents before children)
# ═══════════════════════════════════════════════════════════════════════════════

MIGRATION_ORDER: list[str] = [
    "projects",
    "users",
    "agents",
    "agent_tokens",
    "memories",
    "memory_candidates",
    "memory_index_entries",
    "memory_relations",
    "conversations",
    "messages",
    "raw_events",
    "event_sources",
    "context_packs",
    "context_pack_items",
    "gateway_providers",
    "gateway_capabilities",
    "gateway_bindings",
    "gateway_limits",
    "pipeline_defs",
    "pipeline_runs",
    "review_policies",
    "review_items",
    "audit_events",
    "dead_letters",
    "knowledge_documents",
    "knowledge_blocks",
    "knowledge_chunks",
    "assets",
    "asset_metadata",
    "inbox_items",
    "vault_credentials",
    "vault_access_logs",
    "sessions",
    "admin_events",
]

# ═══════════════════════════════════════════════════════════════════════════════
# Batch sizes
# ═══════════════════════════════════════════════════════════════════════════════

DEFAULT_BATCH_SIZE: int = 500
MAX_BATCH_SIZE: int = 2000
MIN_BATCH_SIZE: int = 10

# ═══════════════════════════════════════════════════════════════════════════════
# SQLite type → display text
# ═══════════════════════════════════════════════════════════════════════════════

SQLITE_TYPE_DISPLAY: dict[str, str] = {
    "INTEGER": "INTEGER → BIGINT",
    "REAL": "REAL → DOUBLE PRECISION",
    "TEXT": "TEXT → TEXT",
    "BLOB": "BLOB → BYTEA",
    "": "→ TEXT",
}

# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════


def generate_run_id() -> str:
    """Generate a short unique run identifier for tracking."""
    return uuid.uuid4().hex[:12]
