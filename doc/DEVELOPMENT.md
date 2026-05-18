<!-- generated-by: gsd-doc-writer -->

# Development Guide

This document covers the development workflow for the Mneme3 platform — from environment setup and code style to adding new features and avoiding common pitfalls.

---

## Local Setup

### Prerequisites

- Python >= 3.10 (CI uses 3.12)
- Node.js >= 20 (CI uses 20)
- PostgreSQL 16 (or Docker)
- Redis 7 (or Docker)

### Clone and Install

```bash
git clone <repo-url>
cd Mneme3

# Backend
cp .env.example .env          # edit DATABASE_URL and REDIS_URL
pip install -e .               # install core
pip install -e ".[dev]"        # install with dev dependencies (pytest, testcontainers)

# Frontend
cd mneme/web
npm install                    # install Vue 3 + Vite + TailwindCSS
```

### Database Setup

```bash
cd mneme/db/alembic
alembic upgrade head           # 71 tables from 28 migrations (baseline 45 + 26 added by migrations)
```

### Run Locally

```bash
# API server
uvicorn mneme.main:app --host 0.0.0.0 --port 8000

# Worker process (separate terminal)
python -m mneme.worker

# Frontend dev server (separate terminal)
cd mneme/web && npm run dev     # http://localhost:5173, proxies /api and /health to :8000
```

### Docker Compose (full stack)

```bash
docker compose up -d                        # postgres + redis + api + worker
docker compose exec api alembic upgrade head # run migrations inside container
docker compose exec api python -m mneme.db.admin_queries create-admin
```

### UNC Path Workaround (NAS development)

The project is on a NAS at `\\192.168.31.28\zyys\letta\Mneme3`. CMD does not support UNC paths as the working directory. All Node.js commands must use a mapped drive:

```powershell
net use X: \\192.168.31.28\zyys\letta\Mneme3\mneme\web /persistent:no
X:
node .\node_modules\vite\bin\vite.js build     # use local vite, not npx
# When done:
C:; net use X: /delete
```

Write and Edit tools are unreliable on the NAS. Use PowerShell `Out-File` for file creation when the Write/Edit tools fail.

---

## Build Commands

| Command | Description |
|---------|-------------|
| `pip install -e .` | Install project in editable mode |
| `pip install -e ".[dev]"` | Install with pytest and testcontainers |
| `alembic upgrade head` | Run all pending migrations (run from `mneme/db/alembic/`) |
| `alembic downgrade -1` | Roll back the most recent migration |
| `alembic revision -m "description"` | Generate a new migration script |
| `uvicorn mneme.main:app --host 0.0.0.0 --port 8000` | Start API server |
| `python -m mneme.worker` | Start background worker process |
| `pytest` | Run full test suite (~58 files, 2400+ tests) |
| `pytest tests/test_auth.py` | Run a single test file |
| `pytest tests/test_auth.py -k test_login` | Run a single test by name |
| `pytest -x -v` | Verbose output, stop on first failure |
| `npm run dev` | Start frontend Vite dev server (from `mneme/web/`) |
| `npm run build` | Type-check and build frontend to `mneme/web/dist/` |
| `npm run typecheck` | Run `vue-tsc --noEmit` for type checking only |
| `npm run preview` | Preview the production build locally |

---

## Code Style

### Python

The project enforces code style via **ruff** (lint + format + import sorting). CI runs three ruff checks on every push:

- `ruff check --select I mneme/` -- import sorting
- `ruff format --check mneme/` -- formatting
- `ruff check mneme/` -- general linting

> **Note:** ruff is NOT listed in `pyproject.toml` dependencies ([project] or [project.optional-dependencies]). It is installed separately in CI (e.g., via `pip install ruff`). If you want to run ruff locally, install it manually with `pip install ruff`.

Key conventions:

| Rule | Requirement |
|------|-------------|
| Python version | >= 3.10 |
| Type annotations | `from __future__ import annotations` + `str \| None` (PEP 604 union syntax) |
| ORM | SQLAlchemy 2.0 declarative (`DeclarativeBase`) |
| Schema | Pydantic v2 (`model_validate`, not `parse_obj`) |
| Time | Always `timestamptz`; never bare `datetime` |
| Primary keys | Always `uuid` |
| JSON columns | Always `jsonb`; never bare `json` |
| Naming | `snake_case` everywhere — tables, columns, fields, API endpoints |
| Imports | Absolute imports: `from mneme.db.agents import ...` |
| Logging | JSON structured: `timestamp / level / request_id / actor_type / route / status_code / duration_ms` |
| API responses | Success envelope: `{"request_id": ..., "correlation_id": ..., "data": ..., "meta": {}}`; Error envelope: `{"request_id": ..., "correlation_id": ..., "error": {"code": ..., "message": ..., "details": {}}}` |
| Error codes | Stable string enum codes, not bare HTTP status codes |
| Config | `get_settings()` returns a plain `Settings()` instance (pydantic-settings); no `@lru_cache` decorator — callers that want singleton behavior should cache it themselves |

### Frontend (Vue 3 / TypeScript)

The frontend enforces code style via **vue-tsc** (type checking) and **Vite** build. CI runs `npx vue-tsc --noEmit` and `npm run build` on every push.

| Rule | Requirement |
|------|-------------|
| Framework | Vue 3 + Composition API + `<script setup>` + TypeScript |
| Build | Vite 6 + TailwindCSS 3 |
| State | Pinia + TanStack Query (Vue Query) |
| Component size | Single-file components <= 600 lines; split into sub-components if larger |
| API client | `apiData<T>(path, options)` and `apiRequest<T>(path, options)` from `@/api/client`; `BASE_URL` is already `/api/v4` |
| Lazy loading | Use dynamic imports for route-level components: `() => import("@/pages/...")` |

---

## Project Structure Walkthrough

```
Mneme3/
├── mneme/                        # Python backend package
│   ├── api/                      # FastAPI application
│   │   ├── router.py             # Main router: registers all 50 route modules under /api/v4
│   │   └── routes/               # Route modules organized by bounded context
│   │       ├── agent/            # Agent, cards, context, conversations, messages
│   │       ├── gateway/          # Provider/model/capability/binding gateway
│   │       ├── knowledge/        # Documents, search, stores, importer, source maps
│   │       ├── memory/           # Memory CRUD, candidates, index, relations, stores, refine, review
│   │       └── system/           # Auth, health, backup, assets, audit, vault, pipelines, etc.
│   ├── db/                       # Database layer
│   │   ├── base.py               # SQLAlchemy engine, SessionLocal, DeclarativeBase, get_db()
│   │   ├── transactions.py       # session_scope() and transaction() context managers
│   │   └── alembic/              # Alembic migrations (28 migration files)
│   ├── backup/                    # pg_dump + manifest + restore engine
│   ├── config.py                 # pydantic-settings: all configuration from env vars
│   ├── context/                   # Context Assembly Engine (Pipeline Orchestrator + Strategy)
│   ├── core/                      # Object Registry
│   ├── domain/                    # Domain objects (object_registry, etc.)
│   ├── eval_engine/               # Evaluation engine
│   ├── gateway/                   # Provider abstraction layer
│   ├── graph_engine/              # Graph engine
│   ├── importer/                  # Import engine (by-asset batch import)
│   ├── knowledge/                # Document/Block/Chunk domain + FTS indexing
│   ├── memory/                   # Memory domain: Extract/FTS/Index + Refine
│   ├── migration/                 # Migration tools (discovery/dumper/loader/manifest/planner/tracker/verifier)
│   ├── observability/             # Health / Logs / Metrics
│   ├── restore/                   # Restore preview engine
│   ├── schemas/                  # Pydantic v2 model definitions
│   ├── search/                    # Global search
│   ├── security/                  # Policy Engine + Review Router + Audit
│   ├── static/                    # Static resources (frontend build artifacts)
│   ├── storage/                   # File staging + storage backend
│   ├── sync/                      # L7 federation sync protocol
│   ├── vault/                     # Fernet encryption + access log
│   ├── worker/                   # Background worker: Dispatcher, Poller, Lease, Retry, DLQ, Consumers, Sweepers
├── tests/                        # pytest test suite (58 files)
├── mneme/web/                    # Vue 3 frontend
│   ├── src/
│   │   ├── pages/                # Route-level page components
│   │   ├── components/           # Reusable UI components (DataTable, Sidebar, etc.)
│   │   ├── router/               # Vue Router config with auth guards
│   │   ├── stores/               # Pinia stores (auth, etc.)
│   │   ├── api/                  # API client layer
│   │   └── composables/          # Vue composables
│   ├── vite.config.ts            # Vite config: proxy /api to :8000, @ alias to src/
│   └── tailwind.config.js        # Tailwind: brand colors, Inter + JetBrains Mono fonts
├── .github/workflows/
│   ├── ci.yml                    # CI: lint (ruff), backend (pytest), frontend (vue-tsc + build), docker
│   └── cd.yml                    # CD: deploy to production on push to main
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml                # Python project metadata, dependencies, setuptools config
└── .env.example                  # Environment variable template
```

---

## How to Add a New API Route

### Step 1: Create the route file

Place it in the appropriate bounded context directory under `mneme/api/routes/`. Follow the existing pattern:

```python
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from mneme.api.context import RequestContext, get_request_context
from mneme.api.schemas import envelope
from mneme.db.base import get_db

router = APIRouter(prefix="/my-resource", tags=["my-tag"])

@router.get("/")
def list_items(
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_request_context),
):
    ...
    return envelope(data=items, request_id=ctx.request_id, correlation_id=ctx.correlation_id)
```

### Step 2: Register the router

Open `mneme/api/router.py` and:

1. Add the import at the top, grouped under the appropriate bounded context comment.
2. Add `api_v4_router.include_router(your_router)` in the corresponding section.

### Step 3: For memory-bounded-context routes

If your route handles memory store data, add it to the `_store_access_dep` injection loop in `router.py` to enforce agent isolation:

```python
for _router in (
    ...,  # existing routers
    your_new_memory_router,
):
    _router.dependencies.append(_store_access_dep)
```

### Key conventions

- Every write endpoint (POST/PATCH/DELETE) **must** require an `Idempotency-Key` header. Use `check_idempotency_key_any()` from `mneme.db.idempotency`.
- Response format: always wrap with `envelope(data=..., request_id=ctx.request_id)`.
- Avoid route path collisions with existing routes. If needed, use a `/v2` suffix pattern.
- Use the `with_actor` function to attach actor information for audit trails. This is a pure function that returns a new `RequestContext` with the actor replaced -- it is NOT a context manager and cannot be used with `with ... as`. Call it as: `ctx = with_actor(ctx, actor=ActorContext(...))`.

---

## How to Add a Database Migration

The project uses **Alembic** with automatic migration generation.

```bash
# 1. Edit or create your SQLAlchemy model class in mneme/db/
# 2. Generate the migration
cd mneme/db/alembic
alembic revision --autogenerate -m "add_my_new_table"

# 3. Review the generated script in versions/
# 4. Apply it
alembic upgrade head

# 5. Roll back if needed
alembic downgrade -1
```

### Migration conventions

- Migration files use sequential numbering: `0026_description.py` (the latest migration at time of writing).
- Always write seed data in **English** — Chinese characters in SQL migrations can cause encoding issues.
- Test the migration end-to-end: `alembic downgrade -1 && alembic upgrade head`.

---

## How to Add a Frontend Page

### Step 1: Create the page component

Create a new `.vue` file in `mneme/web/src/pages/`:

```vue
<script setup lang="ts">
// Vue 3 Composition API + <script setup>
</script>

<template>
  <div class="p-6">
    <h1 class="text-2xl font-bold">My Page</h1>
  </div>
</template>
```

### Step 2: Add the route

Open `mneme/web/src/router/index.ts` and add a route entry:

```typescript
{
  path: "/app/my-page",
  name: "my-page",
  component: () => import("@/pages/MyPage.vue"),
  meta: { title: "My Page", icon: "my-icon", requiresAuth: true },
},
```

- Set `requiresAuth: true` for authenticated pages.
- Set `guest: true` for pages accessible without authentication (e.g., login, prototypes).
- Use lazy loading (`() => import(...)`) for all route-level components.

### Step 3: Add API calls

Use the shared API functions from `mneme/web/src/api/client.ts`. The module exports `apiData<T>(path, options)` (returns just the data field from the envelope) and `apiRequest<T>(path, options)` (returns the full envelope). `BASE_URL` is already `/api/v4`, so only pass the relative path -- do not prefix with `/api/v4` again.

```typescript
import { apiData } from "@/api/client";

const data = await apiData<MyResourceType>("/my-resource");
```

### Frontend conventions

- Mock data goes in a separate `.ts` file, never inline in Vue SFCs.
- Use `crypto.randomUUID()` fallback: the native `crypto.randomUUID()` has poor browser compatibility; use a manual UUID generator instead.
- Component files should not exceed 600 lines. Split large components using Vue's composition pattern.

---

## Transaction Patterns

All database writes must use the project's transaction helpers from `mneme.db.transactions`:

### `session_scope()` — for new, independent transactions

```python
from mneme.db.transactions import session_scope

with session_scope() as db:
    db.add(business_obj)
    db.add(audit_event)
    db.add(event)
    # auto-commit on successful exit, auto-rollback on exception
```

### `transaction()` — for nested or existing transactions

```python
from mneme.db.transactions import transaction

def my_service(db: Session):
    with transaction(db):
        # If db already has an outer transaction, this yields without interfering.
        # If db has no transaction, this begins and commits one.
        db.add(some_obj)
```

### The Audit Triplet Pattern (Iron Law #2)

Every formal write must write three records **in the same transaction**:

1. **Business table** — the actual data (`db.add(business_obj)`)
2. **audit_events** — who did what (`db.add(audit_event)`)
3. **events** — outbox event for async processing (`db.add(event)`)

```python
with session_scope() as db:
    db.add(document)             # business table
    db.add(audit_event)          # audit_events
    db.add(event)                # events (outbox)
    # session_scope handles commit/rollback atomically
```

**Prohibited:**
- Spreading writes across multiple sessions (no atomicity guarantee)
- Waiting on external API calls inside a transaction

---

## The Five Iron Laws

These are non-negotiable architectural constraints. Violating any of them blocks the PR:

| # | Law | Source |
|---|------|--------|
| 1 | PostgreSQL is the sole source of truth. Redis is used only for queue/dispatch acceleration, never as a source of truth. | `doc/架构基线.md` §3.1 |
| 2 | Every formal write must write business table + `audit_events` + `events` in the **same transaction**. | `doc/架构基线.md` |
| 3 | All external model/API calls must go through the Gateway. **No bypassing or direct connections.** | `doc/架构基线.md` |
| 4 | All formal memories must go through Candidate-first + Review. **Agents must never write directly to formal memory.** | `doc/架构基线.md` |
| 5 | Every derived result must pin its upstream version (`document_version` / `chunk_version` / `target_version`). | `doc/一致性设计.md` §10.2 |

---

## Testing

### Running tests

```bash
pytest                              # full suite (~58 files, 2400+ tests)
pytest tests/test_auth.py           # single file
pytest tests/test_auth.py -k test_login  # single test
pytest -x -v                        # verbose, stop on first failure
```

### Test structure

Tests live in the `tests/` directory at the project root. Test files follow the `test_*.py` naming convention. A shared `conftest.py` provides fixtures (database sessions, test clients, authentication helpers).

### CI behavior

The CI workflow (`ci.yml`) runs tests with:

```bash
pytest tests/ -v --tb=short --timeout=120 -x
```

It spins up PostgreSQL 16 and Redis 7 as service containers, then runs the full test suite with a 120-second per-test timeout. Tests must pass for the `backend` job to succeed.

### Writing new tests

1. Create `tests/test_your_feature.py`.
2. Use fixtures from `conftest.py` (database sessions, client, auth).
3. Follow existing patterns: test files typically use `pytest.mark.asyncio` for async FastAPI test client calls.
4. Use the project's `envelope` response format in assertions: check `response.json()["data"]`.

---

## Common Pitfalls

### 1. UNC path issues on NAS

CMD does not support UNC paths (`\\192.168.31.28\...`) as the working directory. All Node.js commands (npm, npx, vite) must run from a mapped drive letter (see [UNC Path Workaround](#unc-path-workaround-nas-development) above).

### 2. Write/Edit tools unreliable on NAS

The file system on the NAS share can cause Write and Edit tools to fail silently. Fall back to PowerShell `Out-File` when this happens.

### 3. Chinese encoding in SQL migrations

Chinese characters in Alembic migration seed data can produce garbled text in the database. Always write migration seed data in English.

### 4. `@milkdown/theme-nord` incompatibility

The `@milkdown/theme-nord` package is incompatible with the current Milkdown setup. Remove it from dependencies if it causes build issues.

### 5. CodeMirror import issues

The `codemirror` meta-package does not export `lineNumbers` or `keymap`. Import these from `@codemirror/view` directly.

### 6. `crypto.randomUUID()` compatibility

`crypto.randomUUID()` has poor cross-browser support. Use a manual UUID generator instead of relying on the Web Crypto API.

### 7. Frontend dist cleanup

Before rebuilding the frontend dist:

```powershell
Get-ChildItem -Path dist -Recurse | Remove-Item -Force
Remove-Item dist -Force
```

A simple `Remove-Item dist -Recurse` can fail due to locked files.

### 8. Deployment file cleanup

When deploying static files to the nginx server, always clean the target directory first:

```bash
echo 606808 | sudo -S rm -rf /var/www/mneme/*
```

Then copy the new build. Overwriting without cleaning can leave stale files.

### 9. API write operations need explicit commit

When using `session_scope()` or `transaction()`, the commit is handled automatically. But if you manage a session manually, you must call `db.commit()` explicitly — SQLAlchemy sessions in this project use `autocommit=False`.

### 10. Route path collisions

New API routes must not share the same method+path combination as existing routes. If a refactor requires path overlap, use a `/v2` suffix (e.g., `POST /documents/v2` instead of `POST /documents`).

### 11. Frontend prototype routes need `guest: true`

Frontend routes for prototype pages (e.g., `/app/knowledge-v2`) must set `guest: true` in the route meta. Without it, the route is blocked by App.vue's auth spinner, which waits indefinitely for authentication state.

```typescript
{
  path: "/app/my-prototype",
  name: "my-prototype",
  component: () => import("@/pages/MyPrototype.vue"),
  meta: { guest: true },
},
```

### 12. Idempotency-Key header required for all writes

Every POST, PATCH, and DELETE endpoint requires an `Idempotency-Key` header in the request. The backend uses `check_idempotency_key_any()` from `mneme.db.idempotency` to enforce this. Forgetting the header will result in a 4xx rejection. In the frontend client, the `apiData` function does not inject this automatically -- callers must add it explicitly via the `headers` option:

```typescript
await apiData("/my-resource", {
  method: "POST",
  body: payload,
  headers: { "Idempotency-Key": generateRequestId() },
});
```

### 13. Mock data in separate .ts files

Frontend mock data (e.g., for prototype pages) must live in a standalone `.ts` file (like `mock-data.ts`), never inline in a Vue SFC. Inlining mocks bloats component files and makes them harder to review.

---

## Branch Conventions

- **Main branch**: `main`
- **Release branches**: `release/**` (CI triggers on pushes to both `main` and `release/**`)
- Branch naming: No strict convention is enforced beyond the CI branch filters. Use descriptive branch names that reflect the change (e.g., `feat/new-feature`, `fix/bug-description`).

---

## PR Process

CI must pass before merging. The CI pipeline runs on every push to `main` and `release/**`, and on every PR targeting `main`:

1. **Lint** — Ruff checks: import sorting (`--select I`), formatting (`format --check`), and linting (`check`).
2. **Backend** — Full pytest suite against PostgreSQL 16 + Redis 7 service containers (requires lint to pass first).
3. **Frontend** — `vue-tsc --noEmit` type-check followed by `npm run build` (Vite production build).
4. **Docker** — Build check: verifies the Docker image builds successfully (requires lint to pass first).

All four checks must pass. The workflow uses `concurrency` groups to cancel redundant runs.

When submitting a PR:

- Ensure all writes follow the audit triplet pattern (Iron Law #2).
- Ensure all external calls go through the Gateway (Iron Law #3).
- Include `Idempotency-Key` headers on all write endpoints.
- Write tests for new functionality following existing patterns in `tests/`.
- Verify that `ruff check mneme/` and `ruff format --check mneme/` pass locally before pushing.
- For frontend changes, verify that `npx vue-tsc --noEmit` passes.
