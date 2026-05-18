# Mneme API Reference

## API Prefix and Versioning

All API endpoints are served under the `/api/v4` prefix. The API is built on FastAPI. Interactive documentation is available at `/docs` (Swagger UI) and `/openapi.json`.

## Standard Response Envelope

### Success

```json
{
  "request_id": "<UUID>",
  "correlation_id": "<UUID>",
  "data": { },
  "meta": {}
}
```

### Error

```json
{
  "request_id": "<UUID>",
  "correlation_id": "<UUID>",
  "error": {
    "code": "auth_required",
    "message": "Human-readable description",
    "details": {}
  }
}
```

## Request Headers

| Header | Required | Description |
|--------|----------|-------------|
| `Idempotency-Key` | **POST/PATCH/DELETE writes** | Unique string for write idempotency. |
| `X-Request-Id` | No | UUID request identifier. Auto-generated if omitted. |
| `X-Correlation-Id` | No | UUID for cross-service correlation. Defaults to `X-Request-Id`. |

## Authentication

Session-based (cookie) for human users, bearer token for agents. Most endpoints require `get_current_user_session`.

---

## Route Index

| Bounded Context | Prefix | File | Count |
|-----------------|--------|------|-------|
| Auth | `/auth` | `system/auth.py` | 3 |
| Agents | `/agents` | `agent/agents.py` | 9 |
| Agent Cards | `/agent-cards` | `agent/agent_cards.py` | 10 |
| Agent Context | `/agent` | `agent/agent_context.py` | 1 |
| Context Compiler | `/context` | `agent/context.py` | 3 |
| Context Assembly | `/context` | `agent/context_assembly.py` | 1 |
| Conversations | `/conversations` | `agent/conversations.py` | 6 |
| Messages | nested under conversations | `agent/messages.py` | 4 |
| Event Sources | nested under conversations | `system/event_source.py` | 2 |
| Raw Events | `/raw-events` | `system/raw_events.py` | 3 |
| Projects | `/projects` | `system/projects.py` | 5 |
| Assets | `/assets` | `system/assets.py` | 13 |
| Asset Metadata | `/asset-metadata` | `system/asset_metadata.py` | 5 |
| Knowledge | `/knowledge` | `knowledge/knowledge.py` | 11 |
| Knowledge Search | `/knowledge` | `knowledge/knowledge_search.py` | 5 |
| Knowledge v2 | mixed | `knowledge_v2.py` | 11 |
| Knowledge Stores | `/sub-libraries` | `knowledge/knowledge_stores.py` | 5 |
| Source Maps | `/source-maps` | `knowledge/source_map.py` | 6 |
| Import v2 | mixed | `import_v2.py` | 2 |
| Import Jobs | `/import` | `knowledge/import_jobs.py` | 4 |
| Importer | `/importer` | `knowledge/importer.py` | 5 |
| Original Pool | `/admin/originals` | `original_pool.py` | 2 |
| Memory | `/memory` | `memory/memory.py` | 24 |
| Memory Candidates | `/memory-candidates` | `memory/memory_candidates.py` | 7 |
| Memory Index | `/memory-index` | `memory/memory_index.py` | 6 |
| Memory Relations | `/memory` | `memory/memory_relations.py` | 6 |
| Memory Stores | `/memory-stores` | `memory/memory_stores.py` | 8 |
| Graph | `/graph` | `memory/graph.py` | 15 |
| Inbox | `/inbox` | `memory/inbox.py` | 5 |
| Neg Space Events | `/neg-space` | `memory/neg_space_events.py` | 4 |
| Refine | `/refine` | `memory/refine.py` | 7 |
| Review Items | `/review/items` | `memory/review_items.py` | 11 |
| Review Policy | `/review/policy` | `memory/review_policy.py` | 6 |
| Gateway | `/gateway` | `gateway/gateway.py` | 24 |
| Vault | `/vault/credentials` | `system/vault.py` | 7 |
| Trust Accounts | `/trust` | `system/trust_accounts.py` | 6 |
| Dashboard | `/dashboard` | `system/dashboard.py` | 2 |
| Health | `/health` | `system/health.py` | 5 |
| Backup/Restore | `/admin` | `system/backup.py` | 10 |
| Dead Letters | `/admin/dead-letters` | `system/dead_letters.py` | 3 |
| Admin Audit | `/admin/audit-events` | `system/admin_audit.py` | 2 |
| Admin Events | `/admin/events` | `system/admin_events.py` | 2 |
| Admin Logs | `/admin/logs` | `system/admin_logs.py` | 1 |
| Migration | `/admin/migrations` | `system/migration.py` | 8 |
| Eval | `/eval` | `system/eval.py` | 15 |
| Pipelines | `/pipelines` | `system/pipelines.py` | 9 |
| Global Search | `/search/global` | `system/global_search.py` | 1 |
| Event Log | `/event-log` | `system/event_log.py` | 2 |
| Graph Triggers | `/graph-triggers` | `system/graph_triggers.py` | 4 |
| Sync | `/sync` | `system/sync.py` | 8 |

**Total: ~325 endpoints across 49 route files.**

---

## Auth

Prefix: `/auth`

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| POST | `/auth/login` | `login` | Authenticate user, set session cookie. |
| POST | `/auth/logout` | `logout` | Revoke session, clear cookie. |
| GET | `/auth/me` | `me` | Return current user + session. |

---

## Agents

Prefix: `/agents`

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| POST | `/agents` | `create_agent_route` | Create agent. Policy: `agent.create`. |
| GET | `/agents` | `list_agents_route` | List agents (paginated). |
| GET | `/agents/{agent_id}` | `get_agent_route` | Get agent by ID. |
| PATCH | `/agents/{agent_id}` | `update_agent_route` | Update agent. Policy: `agent.update`. |
| POST | `/agents/{agent_id}/disable` | `disable_agent_route` | Disable agent. Policy: `agent.disable`. |
| POST | `/agents/{agent_id}/archive` | `archive_agent_route` | Archive agent. Policy: `agent.archive`. |
| POST | `/agents/{agent_id}/tokens` | `create_agent_token_route` | Create bearer token. Returns `token_raw` once. |
| GET | `/agents/{agent_id}/tokens` | `list_agent_tokens_route` | List agent tokens. |
| POST | `/agents/{agent_id}/tokens/{token_id}/revoke` | `revoke_agent_token_route` | Revoke a token. |

---

## Agent Cards

Prefix: `/agent-cards`

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| POST | `/agent-cards` | `create_card_route` | Create agent card. |
| GET | `/agent-cards` | `list_cards_route` | List cards (paginated, filterable by `card_type`). |
| GET | `/agent-cards/{card_id}` | `get_card_route` | Get card by ID. |
| PATCH | `/agent-cards/{card_id}` | `update_card_route` | Update card. |
| DELETE | `/agent-cards/{card_id}` | `archive_card_route` | Archive (soft-delete) card. |
| POST | `/agent-cards/{card_id}/tools` | `create_tool_item_route` | Create tool item under a tool-type card. |
| GET | `/agent-cards/{card_id}/tools` | `list_tool_items_route` | List tool items for a card. |
| GET | `/agent-cards/{card_id}/tools/{item_id}` | `get_tool_item_route` | Get tool item by ID. |
| PATCH | `/agent-cards/{card_id}/tools/{item_id}` | `update_tool_item_route` | Update tool item. |
| DELETE | `/agent-cards/{card_id}/tools/{item_id}` | `archive_tool_item_route` | Archive tool item. |

---

## Agent Context

Prefix: `/agent`

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| POST | `/agent/context` | `agent_context_route` | Assemble context for an agent query. Delegates to context assembly engine. |

---

## Context Compiler

Prefix: `/context`

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| POST | `/context/compile` | `compile_route` | Compile a context pack from knowledge + memories, write to `context_packs`. |
| GET | `/context/packs` | `list_packs_route` | List context packs (paginated, filterable by `agent_id`, `project_id`, `status`). |
| GET | `/context/packs/{pack_id}` | `get_pack_route` | Get context pack detail with items. |

---

## Context Assembly

Prefix: `/context`

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| POST | `/context/assemble` | `assemble_route` | Assemble context via card-based injection strategies. |

Note: Both `context.py` and `context_assembly.py` share prefix `/context`. Their routes coexist: `/compile`, `/packs`, `/packs/{pack_id}`, and `/assemble`.

---

## Conversations

Prefix: `/conversations`

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| POST | `/conversations` | `create_conversation_route` | Create conversation. |
| GET | `/conversations` | `list_conversations_route` | List conversations (paginated, filterable). |
| GET | `/conversations/{conversation_id}` | `get_conversation_route` | Get conversation detail with event sources. |
| PATCH | `/conversations/{conversation_id}` | `update_conversation_route` | Update mutable fields. |
| POST | `/conversations/{conversation_id}/archive` | `archive_conversation_route` | Archive (active -> archived). Sets `ended_at`. |
| POST | `/conversations/{conversation_id}/delete` | `delete_conversation_route` | Soft-delete (status -> 'deleted'). |

---

## Messages

Nested under conversations (no separate prefix).

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| POST | `/conversations/{conversation_id}/messages` | `create_message_route` | Write a single message. Immutable after creation. |
| POST | `/conversations/{conversation_id}/messages/batch` | `create_message_batch_route` | Batch import up to 500 messages. All-or-nothing transaction. |
| GET | `/conversations/{conversation_id}/messages` | `list_messages_route` | List messages, ordered by `message_time` ASC. |
| GET | `/conversations/{conversation_id}/messages/{message_id}` | `get_message_route` | Get single message detail. |

---

## Event Sources

Nested under conversations (no separate prefix).

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| POST | `/conversations/{conversation_id}/event-sources` | `create_event_source_route` | Create event source segment under a conversation. |
| GET | `/conversations/{conversation_id}/event-sources` | `list_event_sources_route` | List all event sources for a conversation. |

---

## Raw Events

Prefix: `/raw-events`

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| POST | `/raw-events` | `create_raw_event_route` | Write raw event. Auto-computes `payload_hash`. |
| GET | `/raw-events` | `list_raw_events_route` | List raw events (filterable by conversation, event_source). |
| GET | `/raw-events/{raw_event_id}` | `get_raw_event_route` | Get raw event by ID. |

---

## Projects

Prefix: `/projects`

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| POST | `/projects` | `create_project_route` | Create project. Formal write path with audit + outbox + idempotency. |
| GET | `/projects` | `list_projects_route` | List projects (paginated, filterable by `status`). |
| GET | `/projects/{project_id}` | `get_project_route` | Get project by ID. |
| **PUT** | `/projects/{project_id}` | `update_project_route` | Update project. Only provided fields changed. |
| DELETE | `/projects/{project_id}` | `archive_project_route` | Archive (soft-delete) project. |

---

## Assets

Prefix: `/assets`

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| POST | `/assets` | `create_asset_route` | Create asset record (`ingest_state='pending'`). |
| GET | `/assets` | `list_assets_route` | List assets (paginated, many filter options). |
| GET | `/assets/{asset_id}` | `get_asset_route` | Get asset by ID. |
| PATCH | `/assets/{asset_id}` | `update_asset_route` | Update asset mutable fields. |
| DELETE | `/assets/{asset_id}` | `delete_asset_route` | Soft-delete (status='deleted'). |
| POST | `/assets/{asset_id}/restore` | `restore_asset_route` | Restore asset to 'active'. |
| PATCH | `/assets/{asset_id}/status` | `change_asset_status_route` | Atomically transition status with state-machine validation. |
| POST | `/assets/{asset_id}/metadata` | `add_asset_metadata_route` | Add/update metadata (upsert). |
| GET | `/assets/{asset_id}/metadata` | `list_asset_metadata_route` | List metadata for asset. |
| GET | `/assets/{asset_id}/metadata/{metadata_id}` | `get_asset_metadata_route` | Get single metadata entry. |
| PATCH | `/assets/{asset_id}/metadata/{metadata_id}` | `update_asset_metadata_route` | Partially update metadata. |
| DELETE | `/assets/{asset_id}/metadata/{metadata_id}` | `delete_asset_metadata_route` | Delete metadata. |
| POST | `/assets/ingest` | `ingest_asset_route` | Full upload-to-asset: validate, stage, dedup, inbox, asset, promote, link. |

---

## Asset Metadata (Standalone)

Prefix: `/asset-metadata`

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| POST | `/asset-metadata` | `create_asset_metadata_route` | Create metadata (requires `asset_id` query param). |
| GET | `/asset-metadata` | `list_asset_metadata_route` | List metadata (requires `asset_id` query param). |
| GET | `/asset-metadata/{metadata_id}` | `get_asset_metadata_route` | Get metadata by ID. |
| PATCH | `/asset-metadata/{metadata_id}` | `update_asset_metadata_route` | Partially update metadata. |
| DELETE | `/asset-metadata/{metadata_id}` | `delete_asset_metadata_route` | Delete metadata. |

---

## Knowledge

Prefix: `/knowledge`

### Documents

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| POST | `/knowledge/documents` | `create_document_route` | Create knowledge document. |
| GET | `/knowledge/documents` | `list_documents_route` | List documents (paginated, filter by project, status, sub_library). |
| GET | `/knowledge/documents/{document_id}` | `get_document_route` | Get single document. |
| PATCH | `/knowledge/documents/{document_id}` | `update_document_route` | Update document fields. |
| POST | `/knowledge/documents/{document_id}/archive` | `archive_document_route` | Archive (active -> archived). |

### Blocks

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| POST | `/knowledge/documents/{document_id}/blocks` | `add_block_route` | Add block. Marks index stale. |
| GET | `/knowledge/documents/{document_id}/blocks` | `list_blocks_route` | List blocks, ordered by `block_order`. |
| PATCH | `/knowledge/blocks/{block_id}` | `update_block_route` | Update block. Recalculates text, marks index stale. |
| DELETE | `/knowledge/blocks/{block_id}` | `delete_block_route` | Delete block. Marks parent index stale. |

### Chunks

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| GET | `/knowledge/documents/{document_id}/chunks` | `list_chunks_route` | List chunks. Optional `document_version` filter. |
| POST | `/knowledge/documents/{document_id}/rechunk` | `rechunk_document_route` | Rechunk document with specified strategy. Deletes old, regenerates. |

---

## Knowledge Search

Prefix: `/knowledge`

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| **GET** | `/knowledge/search` | `search_knowledge_route` | FTS across knowledge chunks. Supports `expand_context`, `sensitivity_floor`. |
| GET | `/knowledge/documents/{document_id}/index-state` | `get_document_index_state_route` | Read index_states row for a document. |
| POST | `/knowledge/indexes/refresh` | `refresh_fts_indexes_route` | Refresh FTS states for stale documents. |
| GET | `/knowledge/citations/{chunk_id}` | `get_chunk_citation_route` | Build citation for a chunk (provenance chain). |
| GET | `/knowledge/documents/{document_id}/citations` | `list_document_citations_route` | List citations for all chunks in a document. |

---

## Knowledge v2

Mixed prefixes.

### Project Backends

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| GET | `/projects/{project_id}/backends` | `list_project_backends` | List backends for a project. |
| PUT | `/projects/{project_id}/backends/{backend_type}` | `toggle_backend` | Enable/disable a backend type. |

### Pipeline Rules

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| GET | `/projects/{project_id}/pipeline-rules` | `get_pipeline_rules` | Get pipeline rules. |
| PUT | `/projects/{project_id}/pipeline-rules` | `update_pipeline_rules` | Replace all pipeline rules. |

### Document Tree

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| GET | `/projects/{project_id}/tree` | `document_tree` | Get document folder tree. |

### Document Content (v2)

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| GET | `/knowledge/documents/{document_id}/v2/content` | `get_document_v2` | Get document with full block/chunk/index data. |
| PATCH | `/knowledge/documents/{document_id}/v2/content` | `update_document_v2` | Update document markdown content. |

### Document Create / Move

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| POST | `/knowledge/documents/v2` | `create_document_v2` | Create empty document (optional initial markdown). |
| POST | `/knowledge/documents/{document_id}/move` | `move_document` | Move document to another project/folder. |

### Index States & Health

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| GET | `/knowledge/documents/{document_id}/index-states` | `document_index_states` | Get per-backend index states. |
| GET | `/projects/{project_id}/health` | `project_health` | Project health: backend status, stale/failed counts. |
| POST | `/projects/{project_id}/indexes/rebuild-stale` | `rebuild_stale_indexes` | Rebuild stale indexes (optionally filtered by backend). |

---

## Knowledge Stores (Sub-Libraries)

Prefix: `/sub-libraries`

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| POST | `/sub-libraries` | `create_sub_library_route` | Register knowledge store backend. |
| GET | `/sub-libraries` | `list_sub_libraries_route` | List stores (filterable by `type`). |
| GET | `/sub-libraries/{lib_id}` | `get_sub_library_route` | Get store by ID. |
| PUT | `/sub-libraries/{lib_id}` | `update_sub_library_route` | Update store registration. |
| DELETE | `/sub-libraries/{lib_id}` | `delete_sub_library_route` | Remove store registration. |

---

## Source Maps

Prefix: `/source-maps`

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| POST | `/source-maps` | `create_source_map_route` | Create source->target provenance mapping. |
| GET | `/source-maps` | `list_source_maps_route` | List source maps (paginated, many filters). |
| GET | `/source-maps/upstream` | `list_upstream_route` | Find sources pointing TO a target. |
| GET | `/source-maps/downstream` | `list_downstream_route` | Find targets derived FROM a source. |
| GET | `/source-maps/{source_map_id}` | `get_source_map_route` | Get source map by ID. |
| DELETE | `/source-maps/{source_map_id}` | `delete_source_map_route` | Hard-delete source map. |

---

## Import v2

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| POST | `/projects/{project_id}/import-v2` | `import_files_v2` | Import files with auto pipeline matching (multipart). |
| GET | `/projects/{project_id}/import-exclusions` | `get_exclusions` | Get import exclusion patterns. |

---

## Import Jobs

Prefix: `/import`

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| POST | `/import` | `create_import_job` | Upload file + create processing job (multipart/form-data). |
| POST | `/import/by-asset` | `create_import_jobs_by_asset` | Create jobs for already-uploaded assets (JSON). |
| GET | `/import/{job_id}/status` | `get_import_job_status` | Poll job status. |
| GET | `/import` | `list_import_jobs` | List jobs (filterable by `asset_id`). |

---

## Importer

Prefix: `/importer`

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| POST | `/importer/dry-run` | `importer_dry_run` | Validate import payload (zero side effects). |
| POST | `/importer/preview` | `importer_preview` | Preview field mapping (no writes). |
| POST | `/importer/import` | `importer_import` | Execute formal import. |
| GET | `/importer/runs` | `list_import_runs_endpoint` | List import runs. |
| GET | `/importer/runs/{run_id}` | `get_import_run_endpoint` | Get import run detail with report. |

---

## Original Pool

Prefix: `/admin/originals`

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| GET | `/admin/originals/stats` | `original_pool_stats` | Pool storage stats (hot/cold/orphan). |
| POST | `/admin/originals/cleanup` | `cleanup_originals` | Move unreferenced originals hot->cold after `max_age_days`. |

---

## Memory

Prefix: `/memory`

### Core CRUD

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| POST | `/memory` | `create_memory_endpoint` | Create memory (status='draft'). |
| POST | `/memory/activate` | `activate_memory_endpoint` | Activate memory from approved candidate. |
| GET | `/memory` | `list_memories_endpoint` | List memories (paginated, many filters). |
| GET | `/memory/search` | `search_memories_endpoint` | Hybrid search (vector + FTS) across memory index. |
| GET | `/memory/search/status` | `search_status_endpoint` | Index state summary for search. |
| GET | `/memory/{memory_id}` | `get_memory_endpoint` | Get memory by ID. |
| PATCH | `/memory/{memory_id}` | `update_memory_endpoint` | Update fields. Increments version. |
| POST | `/memory/{memory_id}/merge` | `merge_memory_endpoint` | Merge target into this memory (survivor absorbs consumed). |
| POST | `/memory/{memory_id}/expire` | `expire_memory_endpoint` | Expire (active -> expired). |
| POST | `/memory/{memory_id}/restore` | `restore_memory_endpoint` | Restore (expired|deleted -> active). |
| DELETE | `/memory/{memory_id}` | `delete_memory_endpoint` | Soft-delete. |

### Batch

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| POST | `/memory/approve` | `batch_approve_endpoint` | Batch approve drafts -> active. |
| POST | `/memory/reject` | `batch_reject_endpoint` | Batch reject drafts -> deleted. |

### Versions

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| GET | `/memory/{memory_id}/versions` | `list_memory_versions_endpoint` | List version history. |
| GET | `/memory/{memory_id}/versions/{version}` | `get_memory_version_endpoint` | Get specific version. |

### Sources

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| GET | `/memory/{memory_id}/sources` | `list_memory_sources_endpoint` | List source links. |
| POST | `/memory/{memory_id}/sources` | `add_memory_source_endpoint` | Add source link. |
| DELETE | `/memory/sources/{memory_source_id}` | `remove_memory_source_endpoint` | Remove source link. |

### Extract

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| POST | `/memory/extract` | `trigger_extract` | Manually trigger Memory Extract Pipeline. |

### Decay & Reinforcement

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| POST | `/memory/decay` | `trigger_decay_endpoint` | Trigger decay batch. |
| GET | `/memory/decay-status` | `decay_status_endpoint` | Decay state summary. |
| POST | `/memory/{memory_id}/reinforce` | `reinforce_memory_endpoint` | Apply reinforcement bonus. |

### Emotion

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| POST | `/memory/emotion-infer` | `trigger_emotion_infer_endpoint` | Trigger emotion inference. |
| GET | `/memory/emotion-status` | `emotion_status_endpoint` | Emotion distribution summary. |

---

## Memory Candidates

Prefix: `/memory-candidates`

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| POST | `/memory-candidates` | `submit_candidate_endpoint` | Submit candidate (idempotent via candidate_hash). |
| GET | `/memory-candidates` | `list_candidates_endpoint` | List candidates (paginated, many filters). |
| GET | `/memory-candidates/{candidate_id}` | `get_candidate_endpoint` | Get candidate by ID. |
| PATCH | `/memory-candidates/{candidate_id}` | `update_candidate_endpoint` | Update candidate. |
| POST | `/memory-candidates/{candidate_id}/approve` | `approve_candidate_endpoint` | Approve (pending_review -> approved). |
| POST | `/memory-candidates/{candidate_id}/reject` | `reject_candidate_endpoint` | Reject (pending_review -> rejected). |
| DELETE | `/memory-candidates/{candidate_id}` | `delete_candidate_endpoint` | Hard-delete candidate. |

---

## Memory Index

Prefix: `/memory-index`

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| GET | `/memory-index/entries` | `list_index_entries_endpoint` | List entries (paginated, filterable). |
| GET | `/memory-index/entries/{entry_id}` | `get_index_entry_endpoint` | Get entry by ID. |
| GET | `/memory-index/states` | `list_index_states_endpoint` | Alias for `/entries` (backward compat). |
| POST | `/memory-index/rebuild-fts` | `rebuild_fts_endpoint` | Rebuild FTS (entry-level or memory-level). |
| POST | `/memory-index/rebuild-vector` | `rebuild_vector_endpoint` | Rebuild vector embedding. |
| GET | `/memory-index/status` | `get_index_status_endpoint` | Aggregated index state summary. |

---

## Memory Relations

Prefix: `/memory`

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| POST | `/memory/relations` | `create_relation_endpoint` | Create relation between two memories. |
| GET | `/memory/{memory_id}/relations` | `list_relations_for_memory_endpoint` | List relations involving a memory. |
| GET | `/memory/relations/{memory_relation_id}` | `get_relation_endpoint` | Get relation by ID. |
| PATCH | `/memory/relations/{memory_relation_id}` | `update_relation_endpoint` | Update relation. |
| POST | `/memory/relations/{memory_relation_id}/resolve` | `resolve_relation_endpoint` | Resolve (active -> resolved). |
| POST | `/memory/relations/{memory_relation_id}/cancel` | `cancel_relation_endpoint` | Cancel (active -> cancelled). |

---

## Memory Stores

Prefix: `/memory-stores`

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| POST | `/memory-stores` | `create_store_route` | Create store. Agent isolation enforced. |
| GET | `/memory-stores` | `list_stores_route` | List stores (filterable by `agent_id`, `unbound_only`). |
| GET | `/memory-stores/by-agent/{agent_id}` | `get_store_by_agent_route` | Get store by agent ID. |
| GET | `/memory-stores/{store_id}` | `get_store_route` | Get store by ID. |
| PATCH | `/memory-stores/{store_id}` | `update_store_route` | Update store. |
| DELETE | `/memory-stores/{store_id}` | `delete_store_route` | Delete store. |
| POST | `/memory-stores/{store_id}/bind/{agent_id}` | `bind_store_route` | Bind store to agent. Admin-only. |
| POST | `/memory-stores/{store_id}/unbind` | `unbind_store_route` | Unbind agent from store. Admin-only. |

---

## Graph

Prefix: `/graph`

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| GET | `/graph` | `get_graph_data_endpoint` | Consolidated graph data (nodes + edges) for visualization. |
| GET | `/graph/nodes` | `list_nodes_endpoint` | List nodes (paginated, filterable). |
| GET | `/graph/nodes/{memory_id}` | `get_node_endpoint` | Get node by memory_id. |
| GET | `/graph/nodes/{memory_id}/neighbors` | `get_node_neighbors_endpoint` | N-hop neighborhood via GraphEngine. |
| GET | `/graph/edges` | `list_edges_endpoint` | List edges (paginated, filterable). |
| GET | `/graph/edges/{relation_id}` | `get_edge_endpoint` | Get edge by relation_id. |
| POST | `/graph/query` | `graph_query_endpoint` | Graph traversal (neighborhood/shortest_path/connected/subgraph/ppr/community). |
| GET | `/graph/summary` | `get_summary_endpoint` | Graph statistics (counts, degree distribution). |
| POST | `/graph/nodes` | `create_node_endpoint` | Create node (wraps memory creation). |
| PATCH | `/graph/nodes/{memory_id}` | `update_node_endpoint` | Update node attributes. |
| POST | `/graph/edges` | `create_edge_endpoint` | Create edge (wraps relation creation). |
| DELETE | `/graph/edges/{relation_id}` | `delete_edge_endpoint` | Cancel edge (active -> cancelled). |
| POST | `/graph/ppr` | `ppr_endpoint` | Personalized PageRank via GraphEngine. |
| POST | `/graph/community` | `community_endpoint` | Community detection (Louvain/Girvan-Newman). |
| POST | `/graph/analyze` | `analyze_endpoint` | Full suite graph analysis (PPR + community). |

---

## Inbox

Prefix: `/inbox`

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| POST | `/inbox` | `create_inbox_route` | Create inbox item (text/url). |
| POST | `/inbox/upload` | `upload_file_route` | Upload file to inbox (multipart/form-data). |
| GET | `/inbox` | `list_inbox_route` | List inbox items (paginated, filterable by project, status). |
| GET | `/inbox/{inbox_item_id}` | `get_inbox_route` | Get inbox item detail. |
| POST | `/inbox/{inbox_item_id}/process` | `process_inbox_route` | Trigger processing (received->staged->linked->processed). |

---

## Negative Space Events

Prefix: `/neg-space`

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| POST | `/neg-space/events` | `create_neg_space_event_endpoint` | Record neg-space event (bypass/delete/silence/refuse/redirect). |
| GET | `/neg-space/events` | `list_neg_space_events` | List events (paginated, many filters). |
| GET | `/neg-space/events/{event_id}` | `get_neg_space_event_detail` | Get event by ID. |
| GET | `/neg-space/summary` | `get_neg_space_summary_endpoint` | Aggregated summary by agent/conversation. At least one filter required. |

---

## Refine

Prefix: `/refine`

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| POST | `/refine/dedup` | `dedup_endpoint` | Detect near-duplicate memories via cosine similarity. |
| POST | `/refine/conflict` | `conflict_endpoint` | Detect semantic conflicts via LLM evaluation. |
| POST | `/refine/merge` | `merge_endpoint` | LLM-assisted smart merge of memories. |
| POST | `/refine/expire/scan` | `expire_scan_endpoint` | Scan for expiration candidates. |
| POST | `/refine/expire/apply` | `expire_apply_endpoint` | Scan AND apply expiration. |
| POST | `/refine/quality` | `quality_endpoint` | Compute quality scores and search weights. |
| POST | `/refine/pipeline` | `pipeline_endpoint` | Run full refine pipeline (dedup->conflict->expire->quality). |

---

## Review Items

Prefix: `/review/items`

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| POST | `/review/items` | `create_review_item_endpoint` | Create review item (status='pending'). |
| GET | `/review/items` | `list_review_items` | List review items (paginated, many filters). |
| GET | `/review/items/{review_item_id}` | `get_review_item_detail` | Get item by ID. |
| POST | `/review/items/{review_item_id}/claim` | `claim_review_item_endpoint` | Claim (pending -> in_review). |
| POST | `/review/items/{review_item_id}/approve` | `approve_review_item_endpoint` | Approve (in_review -> approved). Triggers DLQ/restore. |
| POST | `/review/items/{review_item_id}/reject` | `reject_review_item_endpoint` | Reject (in_review -> rejected). |
| POST | `/review/items/{review_item_id}/cancel` | `cancel_review_item_endpoint` | Cancel (pending|in_review -> cancelled). |

### Batch

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| POST | `/review/items/claim` | `batch_claim_review_items_endpoint` | Batch claim. |
| POST | `/review/items/approve` | `batch_approve_review_items_endpoint` | Batch approve. |
| POST | `/review/items/reject` | `batch_reject_review_items_endpoint` | Batch reject. |
| POST | `/review/items/cancel` | `batch_cancel_review_items_endpoint` | Batch cancel. |

---

## Review Policy

Prefix: `/review/policy`

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| GET | `/review/policy/rules` | `list_rules` | List all routing rules, sorted by priority. |
| GET | `/review/policy/rules/{name}` | `get_rule` | Get rule by name. |
| POST | `/review/policy/rules` | `upsert_rule` | Add/update rule (upsert by name). |
| DELETE | `/review/policy/rules/{name}` | `delete_rule` | Delete rule by name. |
| POST | `/review/policy/reset` | `reset_rules` | Reset to built-in defaults. |
| POST | `/review/policy/evaluate` | `evaluate` | Evaluate action/object combo (dry-run, no writes). |

---

## Gateway

Prefix: `/gateway`

### Providers

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| POST | `/gateway/providers` | `register_provider` | Register provider. |
| GET | `/gateway/providers` | `list_providers` | List providers (paginated, filterable). |
| GET | `/gateway/providers/{provider_id}` | `get_provider` | Get provider by ID. |
| PUT | `/gateway/providers/{provider_id}` | `update_provider_endpoint` | Update provider. |

### Models

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| POST | `/gateway/providers/{provider_id}/models` | `register_model` | Register model. |
| GET | `/gateway/providers/{provider_id}/models` | `list_models` | List models (paginated, filterable). |
| GET | `/gateway/providers/{provider_id}/models/{model_id}` | `get_model` | Get model by ID. |
| PUT | `/gateway/providers/{provider_id}/models/{model_id}` | `update_model_endpoint` | Update model. |

### Capabilities

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| POST | `/gateway/capabilities` | `register_capability` | Create capability. |
| GET | `/gateway/capabilities` | `list_capabilities` | List capabilities (paginated, filterable). |
| GET | `/gateway/capabilities/{capability_id}` | `get_capability` | Get capability by ID. |
| PUT | `/gateway/capabilities/{capability_id}` | `update_capability_endpoint` | Update capability. |
| POST | `/gateway/seed/capabilities` | `seed_capabilities_endpoint` | Initialize predefined capabilities (idempotent). |

### Bindings

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| POST | `/gateway/bindings` | `create_binding` | Create capability binding. |
| GET | `/gateway/bindings` | `list_bindings` | List bindings (paginated, many filters). |
| GET | `/gateway/bindings/{binding_id}` | `get_binding` | Get binding by ID. |
| PUT | `/gateway/bindings/{binding_id}` | `update_binding_endpoint` | Update binding. |

### Unified Call

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| POST | `/gateway/call` | `gateway_call` | Unified external provider call entry. Only way to call providers. |

### Usage Limits

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| POST | `/gateway/limits` | `create_limit` | Create usage limit. |
| GET | `/gateway/limits` | `list_limits` | List limits (paginated, many filters). |
| GET | `/gateway/limits/{limit_id}` | `get_limit` | Get limit by ID. |
| PUT | `/gateway/limits/{limit_id}` | `update_limit` | Update limit. |
| DELETE | `/gateway/limits/{limit_id}` | `delete_limit` | Delete limit. |
| GET | `/gateway/limits/{limit_id}/usage` | `get_limit_usage_endpoint` | Get current usage data for limit. |

---

## Vault

Prefix: `/vault/credentials`

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| POST | `/vault/credentials` | `create_credential_endpoint` | Create encrypted credential. Plaintext immediately encrypted. |
| GET | `/vault/credentials` | `list_credentials` | List credentials (paginated, filterable). No plaintext/ciphertext. |
| GET | `/vault/credentials/{credential_id}` | `get_credential_detail` | Get credential metadata. No plaintext. |
| POST | `/vault/credentials/{credential_id}/reveal` | `reveal_credential` | Decrypt and reveal plaintext. Requires step-up auth + policy + review. |
| PUT | `/vault/credentials/{credential_id}` | `update_credential_endpoint` | Update credential (rotate/status/scope). |
| DELETE | `/vault/credentials/{credential_id}` | `delete_credential_endpoint` | Permanently delete credential. |
| GET | `/vault/credentials/{credential_id}/access-logs` | `list_access_logs` | Paginated access logs. |

---

## Trust Accounts

Prefix: `/trust`

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| POST | `/trust/accounts` | `create_or_get_trust_account_endpoint` | Create/get trust account (unique by subject+capability). |
| GET | `/trust/accounts` | `list_trust_accounts` | List accounts (paginated, filterable). |
| GET | `/trust/accounts/by-subject` | `get_trust_account_by_subject_endpoint` | Get by subject type + ID. |
| GET | `/trust/accounts/{trust_account_id}` | `get_trust_account_detail` | Get by ID. |
| POST | `/trust/accounts/{trust_account_id}/record-call` | `record_call_endpoint` | Record call (updates success_rate, trust_score). |
| POST | `/trust/accounts/{trust_account_id}/record-feedback` | `record_feedback_endpoint` | Record feedback (positive/negative/neutral). |

---

## Dashboard

Prefix: `/dashboard`

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| GET | `/dashboard/stats` | `dashboard_stats` | Aggregated counts: memories, candidates, reviews, documents, agents, activity. |
| GET | `/dashboard/health-summary` | `health_summary` | Lightweight health: DB, Redis, outbox. |

---

## Health

Prefix: `/health`

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| GET | `/health/live` | `live` | Liveness probe. Always "ok". |
| GET | `/health/startup` | `startup` | Startup probe. 503 if startup incomplete. |
| GET | `/health/ready` | `ready` | Readiness probe. Checks DB + Redis. |
| GET | `/health/extended` | `extended` | Full diagnostics (DB, Redis, disk, memory, CPU, DB pool, vector service). |
| GET | `/health/features` | `feature_flags` | Active feature flags for frontend. |

---

## Backup / Restore

Prefix: `/admin`

### Backups

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| GET | `/admin/backups` | `list_backup_manifests` | List backups (paginated, newest first). |
| GET | `/admin/backups/{backup_id}` | `get_backup_detail` | Get backup manifest detail. |
| POST | `/admin/backups/{backup_id}/verify` | `verify_backup_integrity` | Verify backup integrity. |

### Trigger & Restore

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| **POST** | `/admin/backup` | `trigger_backup` | Trigger immediate backup (async job). Track via `GET /admin/jobs/{job_id}`. |
| GET | `/admin/restores` | `list_restore_reports` | List restore reports. |
| POST | `/admin/restores/drill` | `execute_restore_drill` | Execute restore drill (temp DB, no production impact). |
| **POST** | `/admin/restore` | `submit_restore` | Submit restore request (creates review_item with review_type='restore_confirm'). |
| GET | `/admin/restore/{backup_id}/preview` | `preview_restore` | Preview restore: table/row comparison, warnings. |

### Jobs

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| GET | `/admin/jobs` | `list_jobs` | List jobs (paginated, filterable by status). |
| GET | `/admin/jobs/{job_id}` | `get_job_status` | Get job status + logs. |

---

## Dead Letters

Prefix: `/admin/dead-letters`

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| GET | `/admin/dead-letters` | `list_dead_letters` | List dead letters (paginated, many filters). |
| GET | `/admin/dead-letters/{dead_letter_id}` | `get_dead_letter_detail` | Get dead letter by ID. |
| POST | `/admin/dead-letters/{dead_letter_id}/replay` | `submit_dead_letter_replay` | Submit replay. Creates review_item, transitions to 'under_review'. |

---

## Admin Audit Events

Prefix: `/admin/audit-events`

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| **GET** | `/admin/audit-events` | `list_audit_events` | List audit events (paginated, filterable by actor_type, action, result, object_type, time range). |
| GET | `/admin/audit-events/{audit_id}` | `get_audit_event_detail` | Get audit event by ID. |

---

## Admin Events (Outbox)

Prefix: `/admin/events`

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| GET | `/admin/events` | `list_events` | List outbox events (paginated, filterable by event_type, publish_state, aggregate_type, time range). |
| GET | `/admin/events/{event_id}` | `get_event_detail` | Get event with delivery records. |

---

## Admin Logs

Prefix: `/admin/logs`

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| GET | `/admin/logs` | `list_logs` | List `api_call_logs` (paginated, filterable by level/source/since/until/call_type). |

---

## Migration

Prefix: `/admin/migrations`

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| GET | `/admin/migrations` | `list_migrations` | List revisions with status (applied/pending). |
| GET | `/admin/migrations/state` | `migration_state` | State summary (head, applied/pending counts). |
| GET | `/admin/migrations/{revision_id}` | `get_migration_revision` | Get revision detail. Matches by prefix or exact. |
| POST | `/admin/migrations/preview` | `preview_migrations` | Preview pending migrations (dry-run). |
| POST | `/admin/migrations/apply` | `apply_migrations` | Apply migrations (upgrade). Supports `dry_run`, `sql_only`. |
| POST | `/admin/migrations/rollback` | `rollback_migrations` | Rollback (downgrade). Supports `dry_run`. |
| GET | `/admin/migrations/runs` | `list_migration_runs` | List migration run history from audit events. |
| GET | `/admin/migrations/runs/{run_id}` | `get_migration_run` | Get run detail. |

---

## Eval

Prefix: `/eval`

### Tasks

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| GET | `/eval/tasks` | `list_tasks_endpoint` | List tasks (paginated, filterable). |
| POST | `/eval/tasks` | `create_task_endpoint` | Create task. |
| GET | `/eval/tasks/{task_id}` | `get_task_endpoint` | Get task detail with metrics + recent results. |
| POST | `/eval/tasks/{task_id}/run` | `run_task_endpoint` | Start task (pending -> running). |
| POST | `/eval/tasks/{task_id}/cancel` | `cancel_task_endpoint` | Cancel task. |
| GET | `/eval/tasks/{task_id}/results` | `list_results_endpoint` | List results for task (paginated). |

### Datasets & Scoring

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| GET | `/eval/datasets` | `list_eval_datasets_endpoint` | List benchmark datasets. |
| POST | `/eval/run` | `run_eval_endpoint` | Run evaluation with auto-scoring. |
| POST | `/eval/score` | `score_endpoint` | Compute metrics (ranking: P@k, R@k, NDCG, MRR, MAP; text: BLEU, ROUGE-L). |

### A/B Testing

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| GET | `/eval/ab-tests` | `list_ab_tests_endpoint` | List A/B tests (paginated, filterable). |
| POST | `/eval/ab-tests` | `create_ab_test_endpoint` | Create A/B test. Variants must use same task_type. |
| GET | `/eval/ab-tests/{ab_test_id}` | `get_ab_test_endpoint` | Get A/B test with per-metric deltas. |
| POST | `/eval/ab-tests/{ab_test_id}/run` | `run_ab_test_endpoint` | Execute A/B comparison (persists deltas with significance tests). |
| POST | `/eval/ab-tests/{ab_test_id}/cancel` | `cancel_ab_test_endpoint` | Cancel A/B test. |
| POST | `/eval/compare` | `compare_endpoint` | Ad-hoc A/B comparison (no persistence). |

---

## Pipelines

Prefix: `/pipelines`

### Definitions

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| POST | `/pipelines/defs` | `create_pipeline_def_route` | Create pipeline definition. |
| GET | `/pipelines/defs` | `list_pipeline_defs_route` | List definitions (paginated, filterable). |
| GET | `/pipelines/defs/{pipeline_def_id}` | `get_pipeline_def_route` | Get definition by ID. |
| PATCH | `/pipelines/defs/{pipeline_def_id}` | `update_pipeline_def_route` | Update definition. |

### Runs

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| POST | `/pipelines/runs` | `create_pipeline_run_route` | Trigger pipeline run. |
| GET | `/pipelines/runs` | `list_pipeline_runs_route` | List runs (paginated, many filters). |
| GET | `/pipelines/runs/{run_id}` | `get_pipeline_run_route` | Get run detail with job and definition. |
| PATCH | `/pipelines/runs/{run_id}/status` | `advance_run_status_route` | Advance status with state-machine validation + optimistic concurrency. |
| POST | `/pipelines/runs/{run_id}/cancel` | `cancel_pipeline_run_route` | Cancel run (pending|running -> cancelled). |

---

## Global Search

Prefix: `/search/global`

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| GET | `/search/global` | `global_search` | Aggregated search across agents, knowledge, memories. Supports LLM query rewriting, cross-encoder re-ranking, PPR graph traversal, temporal cluster search. |

---

## Event Log

Prefix: `/event-log`

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| GET | `/event-log/stream/{stream_type}/{stream_id}` | `replay_stream` | Replay events for a stream, ordered by version ASC. |
| GET | `/event-log/search` | `search_event_log` | Search event log (filterable by project, stream_type, event_type, time range). |

---

## Graph Triggers

Prefix: `/graph-triggers`

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| GET | `/graph-triggers/log` | `get_trigger_log` | Read sync trigger log (filterable by memory_id, trigger_event, action). |
| GET | `/graph-triggers/dependents/{memory_id}` | `get_memory_dependents` | Find graph-dependent nodes for a memory. |
| POST | `/graph-triggers/backfill/{memory_id}` | `trigger_backfill_single` | Sync single memory to graph_nodes. |
| POST | `/graph-triggers/backfill` | `trigger_backfill_all` | Backfill all active memories into graph_nodes. |

---

## Sync (Federation)

Prefix: `/sync`

### Nodes

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| GET | `/sync/nodes` | `list_nodes` | List peer nodes (filterable by status). |
| POST | `/sync/nodes` | `register_node` | Register peer node. |
| GET | `/sync/nodes/{node_id}` | `get_node` | Get node details. |
| PUT | `/sync/nodes/{node_id}` | `update_node` | Update node. |
| DELETE | `/sync/nodes/{node_id}` | `remove_node` | Remove node. |

### Handshake & Push

| Method | Path | Function | Description |
|--------|------|----------|-------------|
| POST | `/sync/handshake` | `handshake` | Receive handshake from remote node (auto-registers). |
| POST | `/sync/push` | `receive_sync_push` | Receive sync push. Validates, enqueues inbound entries. |
| GET | `/sync/queue` | `get_sync_queue` | List sync queue (filterable by direction, status, node_id). |

---

## Common Error Codes

| Code | HTTP | Description |
|------|------|-------------|
| `bad_request` | 400 | Invalid request parameters or payload. |
| `auth_required` | 401 | Authentication required. |
| `permission_denied` | 403 | Policy engine denied the action. |
| `step_up_required` | 403 | Elevated authentication required. |
| `not_found` | 404 | Resource not found. |
| `idempotency_conflict` | 409 | Idempotency key clash or duplicate resource. |
| `review_required` | 202 | Operation requires review approval before execution. |
| `dependency_unavailable` | 503 | Required dependency (DB/Redis) unavailable. |
| `gateway.provider_error` | 502 | External provider call failed. |
| `gateway.provider_timeout` | 504 | External provider call timed out. |
| `gateway.budget_denied` | 402 | Call exceeded budget/usage limits. |
| `internal_error` | 500 | Unexpected server error. |
