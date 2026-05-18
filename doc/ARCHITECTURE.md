<!-- generated-by: gsd-doc-writer -->

---
title: Architecture
description: System architecture and component overview for Mneme 0.1.0
---

# Mneme 0.1.0 -- Architecture

## System Overview

Mneme is a Web-first, Local-first, API-first personal intelligent asset control plane. It provides unified management of assets, knowledge, memory, model/API access, agent integration, permissions, auditing, backup, migration, and system governance. The system follows a layered, domain-driven architecture with a FastAPI REST layer, a PostgreSQL-based source of truth, a Redis-backed worker runtime, and a Vue 3 single-page application frontend.

Mneme is **not** a chatbot shell, a multi-agent orchestration runtime, or a long-term memory store. It is a personal digital asset ledger, a knowledge and memory governance hub, a controlled external memory and context center for agents, and a unified gateway and audit entry point for all model/API calls.

## Five Iron Laws

These five rules govern every design decision in the system:

1. **PostgreSQL is the sole source of truth.** Redis is only a queue and dispatch accelerator -- never a source of truth.
2. **Every formal write must atomically write** the business table, `audit_events`, and `events` (outbox) in a single PostgreSQL transaction.
3. **All external model/API calls must go through the Gateway.** No bypass or direct connection to providers.
4. **All formal memories must pass through Candidate-first + Review.** Agents may never write directly to formal memory.
5. **Every derived result must pin upstream versions** (e.g., `document_version`, `chunk_version`, `target_version`).

## Component Diagram

```text
┌─────────────────────────────────────────────────────────────┐
│  Web UI (Vue 3 + Vite)  │  External Agent  │  API Client   │
└─────────────────────────────────┬───────────────────────────┘
                                  │
                             ┌────▼────┐
                             │ /api/v4 │  (FastAPI + 50 route files, ~326 endpoints)
                             └────┬────┘
                                  │
                    ┌─────────────▼─────────────┐
                    │      Core Kernel          │
                    │  Auth / Session / Token    │
                    │  Policy Engine (RBAC)      │
                    │  Audit                     │
                    │  Object Registry           │
                    │  Outbox Events             │
                    │  Store-Access Middleware   │
                    └─────────────┬─────────────┘
                                  │
         ┌────────────────────────┼────────────────────────┐
         │                        │                        │
    ┌────▼─────┐  ┌──────────┐  ┌▼──────────┐   ┌────────▼────────┐
    │ Asset OS │  │Knowledge │  │ Memory OS │   │ Review Center   │
    │  Inbox   │  │   OS     │  │ Candidate │   │ (cross-cutting) │
    └──────────┘  │ Document │  │  Extract  │   └─────────────────┘
                  │  Chunk   │  │  FTS/Idx  │
                  │ Citation │  │  Refine   │
                  │  Import  │  └───────────┘
                  └──────────┘
         ┌────────────────────────┬────────────────────────┐
    ┌────▼──────┐  ┌──────────┐  ┌▼───────────┐  ┌────────▼────────┐
    │  Gateway  │  │  Agent   │  │  Context   │  │  Graph Engine   │
    │ Provider  │  │ Center   │  │  Compiler  │  │  (PPR + Search) │
    │  Vault    │  └──────────┘  └────────────┘  └─────────────────┘
    └───────────┘

         ┌────────────────────────┬────────────────────────┐
    ┌────▼──────┐         ┌───────▼──────┐       ┌────────▼─────────┐
    │PostgreSQL │         │    Redis     │       │  MnemeData FS    │
    │ (71 tbls) │         │ (queue+lock) │       │  (file staging)  │
    └───────────┘         └──────────────┘       └──────────────────┘

         ┌──────────────────────────────────────────────────────────┐
         │                    Worker Runtime                         │
         │  Dispatcher → Poller → Consumers → Lease → Retry → DLQ   │
         │  Sweepers: Decay / Emotion / Sublimation / Recall / ...  │
         └──────────────────────────────────────────────────────────┘
                                   │
                          External Providers
                     (via Gateway only -- never direct)
```

## Data Flow

A typical request through the system follows this path:

1. **Ingress** -- HTTP request arrives at the FastAPI application (`mneme/main.py`). Middleware executes in order: Request Context (correlation IDs) → Prometheus Metrics → CORS → Store-Access Isolation → Access Logging.

2. **Routing** -- The request matches a route registered in `api_v4_router` (`mneme/api/router.py`), which includes 50 route files under `/api/v4`.

3. **Policy Check** -- Before domain logic executes, the Policy Engine (`mneme/security/policy.py`) evaluates `can(actor, action, object, context)` returning `allow`, `deny`, `review_required`, or `step_up_required`. RBAC roles (owner/operator/viewer/auditor), project scope, capability scope, and sensitivity ceilings are checked.

4. **Domain Logic** -- The route handler performs business operations within a `session_scope()` or `transaction()` context. ALL formal writes follow the triplet pattern:

   ```python
   with session_scope() as db:
       db.add(business_obj)      # Business table
       db.add(audit_event)       # audit_events
       db.add(event)             # events (outbox)
       # commit is automatic at context exit
   ```

5. **Outbox Dispatch** -- The committed `events` row transitions through `pending → dispatching → dispatched`. The Worker polls `events` with `publish_state = 'pending'` and dispatches to registered consumers.

6. **Worker Processing** -- Consumers (ReviewEventConsumer, PipelineEventConsumer, MemoryEventConsumer) process events asynchronously. Failed deliveries are retried with exponential backoff by the RetrySweeper; exhausted deliveries are promoted to `dead_letters`.

7. **External Calls** -- When a consumer or route handler needs external model/API access, it MUST go through `Gateway.call()` (`mneme/gateway/call.py`). The Gateway resolves capability bindings, checks budget, resolves vault credentials, executes the HTTP request via httpx, records the call log, and releases the budget.

8. **Response** -- The API returns a standardized envelope: `{"code": "...", "message": "...", "data": ..., "request_id": "..."}`.

## Key Abstractions

| Abstraction | File | Description |
|---|---|---|
| `session_scope()` / `transaction()` | `mneme/db/transactions.py` | Transaction boundary helpers ensuring all writes are atomic in PostgreSQL |
| `Gateway.call()` | `mneme/gateway/call.py` | Single entry point for all external provider API calls; enforces budget, credential resolution, and audit logging |
| `can()` | `mneme/security/policy.py` | Policy Engine decision function: returns `allow`, `deny`, `review_required`, or `step_up_required` |
| `assemble_context()` | `mneme/context/assembly_engine.py` | Context Assembly entry-point using strategy pattern + pipeline orchestration for agent context |
| `Dispatcher` | `mneme/worker/dispatcher.py` | Worker dispatcher that polls the outbox and routes events to registered consumers |
| `LeaseManager` | `mneme/worker/lease.py` | Redis-based lease acquisition for worker leader election and heartbeat |
| `Settings` | `mneme/config.py` | Pydantic v2 settings loaded from environment variables (pydantic-settings) |
| `StoreAccessMiddleware` | `mneme/api/dependencies/store_access.py` | Ensures agent isolation: Agent A cannot read/write Agent B's memory stores |
| `CredentialVault` | `mneme/vault/` | Fernet envelope encryption for provider credentials with access logging |
| `PipelineOrchestrator` | `mneme/context/pipeline/orchestrator.py` | Composable pipeline steps for context assembly with reorder/replace/extension support |

## Consistency Model

Mneme uses a three-tier consistency model (detailed in `doc/一致性设计.md`):

- **Intra-aggregate strong consistency** -- Business writes, version bumps, audit events, and outbox events all commit in a single PostgreSQL transaction.
- **Derived read-model eventual consistency** -- Chunks, embeddings, index states, graph nodes, and context packs are built asynchronously by workers. They are state-visible, version-trackable, retryable, and stale results remain readable during rebuild.
- **External call non-transactional** -- Provider calls, OCR, embedding, and LLM invocations never hold database transactions. Pattern: local commit → outbox dispatch → worker external call → result write-back → failure compensation.

## Directory Structure Rationale

| Directory | Purpose |
|---|---|
| `mneme/api/` | FastAPI route layer. `routes/` is organized into bounded contexts: `agent/`, `gateway/`, `knowledge/`, `memory/`, `system/`. `router.py` registers all 50 route files under the `/api/v4` prefix. |
| `mneme/db/` | SQLAlchemy 2.0 raw queries (one file per domain aggregate) + Alembic migrations (28 migrations creating 71 tables). Not an ORM model layer -- query functions use `sqlalchemy.text` + `bindparam`. |
| `mneme/config.py` | Centralized pydantic-settings; all configuration from environment variables with defaults and validation. |
| `mneme/schemas/` | Pydantic v2 schemas shared across the API and domain layers. |
| `mneme/security/` | Policy Engine (`policy.py`), Review Router (`review_router.py`), and Audit (`audit.py`). Cross-cutting governance, not domain-scoped. |
| `mneme/context/` | Context Assembly Engine with strategy pattern (`strategies/`) and composable pipeline (`pipeline/`). Converts memory stores + agent cards into token-budgeted context strings. |
| `mneme/memory/` | Memory OS: extract pipeline, FTS search, index manager, embedding, emotion inference, time decay, spontaneous recall, sublimation, PPR graph traversal, temporal clustering. |
| `mneme/knowledge/` | Knowledge OS: document/block/chunk management, FTS, citation tracking, chunking, token estimation. Knowledge module redesign added project-scoped backends. |
| `mneme/gateway/` | Provider call entry (`call.py`) and vault credential bridge (`vault_bridge.py`). The only allowed path for external API calls. |
| `mneme/worker/` | Worker runtime: `app.py` (entry), `dispatcher.py` (poll + dispatch), `lease.py` (Redis leader election), `poller.py` (outbox scan), `retry_sweeper.py`, `recovery_sweeper.py`, plus domain consumers and sweepers. |
| `mneme/storage/` | File staging and storage backend abstraction. Local filesystem (`MnemeData`) is the primary backend; S3-compatible storage is reserved for future. |
| `mneme/vault/` | Fernet-based envelope encryption for provider credentials with access log auditing. |
| `mneme/importer/` | Batch import engine (by-asset) with pipeline matching. |
| `mneme/backup/` | pg_dump-based backup engine with manifest tracking and restore. |
| `mneme/observability/` | Structured logging (JSON with timestamp/level/request_id/actor_type/route/status_code/duration_ms), Prometheus metrics, health checks. |
| `mneme/web/` | Vue 3 + TypeScript frontend built with Vite, styled with TailwindCSS, state managed with Pinia + TanStack Query. |
| `mneme/domain/` | Domain objects and object registry bindings for cross-cutting domain logic. |
| `mneme/core/` | Core Object Registry providing unified service discovery and dependency injection. |
| `mneme/eval_engine/` | Evaluation engine for running and tracking evaluation tasks and results. |
| `mneme/graph_engine/` | Graph engine for entity relationship extraction, PPR traversal, and knowledge graph construction. |
| `mneme/migration/` | Migration tooling: discovery, dumper, loader, manifest, planner, tracker, and verifier. |
| `mneme/restore/` | Restore preview engine for backup restoration planning and validation. |
| `mneme/search/` | Global search across all domains with suggestion support. |
| `mneme/sync/` | L7 federation sync protocol for cross-instance data synchronization. |
| `mneme/static/` | Static file serving for frontend production build artifacts. |
| `tests/` | pytest-based test suite (59 files, 2,400+ test functions) covering API routes, domain logic, and worker behavior. |
| `doc/` | Chinese-language authoritative design documents (architecture baseline, data model, consistency design, migration reference). |

## Technical Stack

| Layer | Technology | Details |
|---|---|---|
| API Framework | FastAPI | Uvicorn ASGI server, `/api/v4` prefix |
| Database | PostgreSQL 16 + pgvector | 71 tables, JSONB, GIN indexes, HNSW/IVFFlat vector indexes |
| ORM / Migrations | SQLAlchemy 2.0 + Alembic | 28 migrations (0001-0026) |
| Schema Validation | Pydantic v2 | `model_validate()` pattern, `pydantic-settings` for config |
| Queue / Locking | Redis 7 | Outbox polling, worker lease, leader election |
| Worker Runtime | Custom (Python) | Dispatcher + Poller + Lease + Retry + DLQ + 7 sweepers |
| Frontend | Vue 3 + Vite + TailwindCSS | Pinia state, TanStack Query, Vue Router, TypeScript |
| Encryption | cryptography (Fernet) | Envelope encryption for vault credentials |
| Observability | Prometheus + Grafana | Structured JSON logging, health probes, cAdvisor, postgres/redis exporters |
| Deployment | Docker Compose | Core services: postgres, redis, api, worker + observability stack |
| Python Runtime | Python >=3.10 | `from __future__ import annotations` for PEP 604 union syntax |
