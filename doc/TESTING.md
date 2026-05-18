<!-- generated-by: gsd-doc-writer -->

# TESTING.md — Mneme3 Testing Guide

---

## Test Framework and Setup

Mneme3 uses **pytest** (>= 7.0) as its primary test framework, with **pytest-asyncio** (>= 0.21) for async support and **testcontainers** (>= 4.0) for optional PostgreSQL-backed integration tests.

Dependencies are declared in `pyproject.toml` under `[project.optional-dependencies] dev`:

| Dependency | Version | Purpose |
|---|---|---|
| pytest | >= 7.0 | Test runner and assertion framework |
| pytest-asyncio | >= 0.21 | Async test support |
| testcontainers | >= 4.0 | Ephemeral PostgreSQL containers for CI parity |

To install all test dependencies:

```bash
pip install -e ".[dev]"
```

Before running any tests, ensure you have set `DATABASE_URL` and `REDIS_URL` (the conftest defaults to SQLite :memory: if these are unset — see "Database Modes" below).

---

## Running Tests

### Full Suite

```bash
pytest
```

Runs all 58 test files (~2,400+ test functions). Defaults to SQLite :memory: with StaticPool.

### Verbose, Fail-Fast

```bash
pytest -x -v
```

`-x` stops on the first failure. `-v` prints each test name and its result.

### Single File

```bash
pytest tests/test_auth.py
pytest tests/test_knowledge.py
pytest tests/test_memory_candidates.py
```

### Single Test by Name (Keyword)

```bash
pytest tests/test_auth.py -k test_login
pytest -k "test_transaction_commits"
```

### Against Real PostgreSQL (Testcontainers)

```bash
pytest --postgres
# or equivalently:
USE_TESTCONTAINERS=1 pytest
```

This starts a `pgvector/pgvector:pg16` container, runs Alembic migrations, rebinds the engine, and tears down after the session.

### With Timeout (CI-style)

```bash
pytest tests/ -v --tb=short --timeout=120 -x
```

---

## Database Modes

The test suite supports two database backends, managed entirely by `tests/conftest.py`:

### SQLite :memory: (default)

- **Connection strategy**: StaticPool — a single shared connection ensures all `SessionLocal()` calls see the same in-memory database.
- **Schema**: Conftest creates SQLite-compatible DDL for the core tables during `pytest_configure`. PostgreSQL-specific functions (`gen_random_uuid()`, `now()`) are registered as SQLite user-defined functions.
- **Adapters**: UUID and dict adapters are registered so Python types bind correctly to SQLite.
- **Use case**: Fast, offline, no Docker required. Suitable for unit tests and CI runs where a full PostgreSQL is unavailable.

### Testcontainers PostgreSQL

- **Activation**: `pytest --postgres` or `USE_TESTCONTAINERS=1`
- **Container**: `pgvector/pgvector:pg16`
- **Schema**: Alembic migrations run against the container, ensuring full production schema fidelity.
- **Engine rebinding**: The global `mneme.db.base.engine` and `SessionLocal` are rebound to the container after startup.
- **Use case**: Integration tests requiring `pgvector`, `jsonb`, real FK enforcement, or Alembic-specific DDL.

### Remote PostgreSQL

You can also target a shared PostgreSQL instance via environment variables:

| Variable | Default |
|---|---|
| `MNEME_TEST_DB_HOST` | (none) |
| `MNEME_TEST_DB_PORT` | `5432` |
| `MNEME_TEST_DB_USER` | `mneme` |
| `MNEME_TEST_DB_PASSWORD` | (empty) |
| `MNEME_TEST_DB_NAME` | `mneme` |

When `MNEME_TEST_DB_HOST` is set (and `--postgres` is not), the conftest connects to the specified PostgreSQL and expects the schema to already exist.

---

## Test Structure

All tests live under `tests/` at the project root. They are organized by domain, matching the `mneme/` source layout:

| Category | Example Files |
|---|---|
| **Auth & Sessions** | `test_auth.py` |
| **Agents** | `test_agent_lifecycle.py`, `test_agents_lifecycle.py` |
| **Assets & Inbox** | `test_assets.py`, `test_inbox_assets.py` |
| **Knowledge** | `test_knowledge.py`, `test_knowledge_chunking.py`, `test_citation.py` |
| **Memory** | `test_memories.py`, `test_memory_candidates.py`, `test_memory_extract.py`, `test_memory_index.py`, `test_memory_refine.py`, `test_memory_relations.py`, `test_memory_versions.py`, `test_memory_auto_extract.py` |
| **Gateway** | `test_gateway_call.py`, `test_gateway_providers.py` |
| **Vault** | `test_vault_encryption.py`, `test_vault_access_log.py`, `test_vault_gateway_review_integration.py` |
| **Review** | `test_review_items.py`, `test_review_router.py`, `test_review_workflow.py` |
| **Worker** | `test_worker.py`, `test_retry_sweeper.py` |
| **DLQ** | `test_dlq.py`, `test_dlq_replay.py` |
| **Backup/Restore/Migration** | `test_backup.py`, `test_backup_restore_api.py`, `test_restore.py`, `test_migration.py` |
| **Pipeline** | `test_pipelines.py`, `test_pipeline_registry.py`, `test_processing_jobs.py` |
| **Context** | `test_context_assembly.py`, `test_context_compile.py` |
| **Transactions** | `test_transactions.py` |
| **Schemas** | `test_schemas.py` |
| **Integration/Regression** | `test_r1_dashboard.py` through `test_r5_system.py`, `test_p8_integration.py` |
| **Others** | `test_health_logging_metrics.py`, `test_objects.py`, `test_pg_arrays.py`, `test_policy.py`, `test_budget.py`, `test_storage_layer.py`, `test_importer.py`, `test_conversations.py`, `test_messages.py`, `test_raw_events.py`, `test_search_quality.py`, `test_index_lifecycle.py` |

---

## Key Fixtures

Defined in `tests/conftest.py`:

### `db`

```python
@pytest.fixture
def db():
    """Per-test independent session. Commits are not auto-begun; rollback at end."""
    from mneme.db.base import SessionLocal
    session = SessionLocal()
    try:
        yield session
        session.rollback()
    finally:
        session.close()
```

Yields a SQLAlchemy `Session` that is automatically rolled back after each test, providing isolation without manual cleanup.

### `test_user_id` (autouse)

Ensures a well-known test user (`00000000-0000-0000-0000-000000000001`) exists in the `users` table for FK constraint satisfaction. Inserted with `ON CONFLICT DO NOTHING` for idempotency. Because this fixture is `autouse=True`, every DB-backed test gets this user automatically.

### `postgres_container` (session-scoped, autouse)

Starts a PostgreSQL container when `--postgres` is passed. Runs Alembic migrations, seeds test users, and rebinds the global engine. Active only for testcontainers mode.

---

## Testing Patterns

### 1. API Client Testing (FastAPI TestClient)

Tests that verify HTTP endpoint behavior use `fastapi.testclient.TestClient` with `app.dependency_overrides` to inject a test database session.

```python
# Pattern from test_auth.py
app = create_app()

def override_get_db():
    db = Session(engine, expire_on_commit=False)
    try:
        yield db
    finally:
        db.close()

app.dependency_overrides[get_db] = override_get_db

with TestClient(app) as client:
    response = client.post("/auth/login", json={...})
    assert response.status_code == 200
```

### 2. Database-Layer Testing (DAL Functions)

Tests that verify data access logic call DAL functions directly with a `db` session fixture and a `RequestContext` fixture for actor/request metadata.

```python
# Pattern from test_memory_candidates.py
def test_submit_candidate(db, test_context, test_project):
    data = MemoryCandidateCreate(
        project_id=test_project,
        title="Test Memory",
        candidate_text="Some content...",
    )
    candidate = submit_candidate(db, test_context, data)
    assert candidate.candidate_id is not None
    assert candidate.candidate_status == CandidateStatus.PENDING_REVIEW
```

### 3. Pure Unit Testing

Tests that verify algorithm correctness, data structures, or utility functions use standard pytest with no DB dependency. Mock objects and `unittest.mock.patch` are used for external dependencies.

```python
# Pattern from test_review_router.py
def test_action_pattern_exact_match():
    r = ReviewRouteRule(name="test", action_pattern="write", review_type="manual")
    assert r.matches(action="write") is True
    assert r.matches(action="read") is False
```

### 4. Integration Testing (with Mock HTTP)

Gateway tests combine real DB setup with `httpx` mocks to verify the full call chain: budget reservation -> capability lookup -> credential resolution -> provider call -> budget commit.

```python
# Pattern from test_gateway_call.py
with patch("httpx.Client.send") as mock_send:
    mock_response = Mock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.json.return_value = {"choices": [...]}
    mock_send.return_value = mock_response

    result = gateway.call(db, request_context, call_request)
    assert result.status == "success"
```

### 5. Idempotency-Key Pattern

All write-oriented API tests include an `Idempotency-Key` header (or `idempotency_key` field in internal DAL calls). Tests verify that duplicate submissions with the same key return the existing record rather than creating duplicates.

```python
response1 = client.post("/api/v4/knowledge/documents", json={...},
                        headers={"Idempotency-Key": str(uuid4())})
response2 = client.post("/api/v4/knowledge/documents", json={...},
                        headers={"Idempotency-Key": response1.json()["data"]["idempotency_key"]})
assert response2.json()["data"]["document_id"] == response1.json()["data"]["document_id"]
```

---

## Writing New Tests

### File Naming Convention

- Test files: `test_<module>.py` in the `tests/` directory.
- Test functions: `test_<behavior_description>` within classes or at module level.
- Test classes (optional, for grouping): `class Test<Feature>:`.

### Test Fixture Conventions

- Use the shared `db` fixture from conftest for DB-backed tests.
- Create local fixtures for project/test data seeding (e.g., `test_project`, `test_context`).
- Use `autouse` fixtures sparingly — only for universally needed setup (like `test_user_id`).
- Isolate each test: the `db` fixture rollback guarantees no cross-test data leakage.

### Assertion Style

Use plain `assert` statements. Failures are reported by pytest with introspection.

```python
assert result.status == "active"
assert result.created_at is not None
assert len(items) == expected_count
```

---

## Coverage Requirements

No minimum coverage threshold is configured in `pyproject.toml` or pytest configuration. The CI workflow (`ci.yml`) attempts to upload a `htmlcov/` artifact but the test command does not currently include `--cov` flags. Coverage measurement requires manual invocation:

```bash
pytest --cov=mneme --cov-report=html
```

This produces `htmlcov/index.html` for per-file line coverage inspection.

---

## CI Integration

Tests run in GitHub Actions via `.github/workflows/ci.yml`. The workflow triggers on:

- **Push** to `main` or `release/**` branches
- **Pull Request** against `main`

### Job: `lint` (blocking gate for all other jobs)
- Runs `ruff check --select I` (import sorting)
- Runs `ruff format --check` (code formatting)
- Runs `ruff check` (full lint)

### Job: `backend` (Python tests)
- **Depends on**: `lint` passing
- **Services**: `postgres:16` + `redis:7-alpine` (sidecar containers)
- **Python version**: 3.12 (matrix-ready)
- **Test command**:
  ```bash
  pytest tests/ -v --tb=short --timeout=120 -x
  ```
- **Environment**:
  - `DATABASE_URL`: `postgresql://mneme:mneme123@localhost:5432/mneme`
  - `REDIS_URL`: `redis://localhost:6379/0`

### Job: `frontend` (Vue 3 / Vite build)
- Runs `npm ci` then `npx vue-tsc --noEmit` (type check) then `npm run build`
- Produces a `frontend-dist` artifact

### Job: `docker`
- Validates the Docker image builds successfully (does not push)

All jobs use concurrency groups so new commits cancel in-progress CI runs on the same branch/PR.

---

## Code Review Rules

Refer to `REVIEW.md` in the project root for the complete review specification. Key rules relevant to test development:

### Defect Classification

| Grade | Definition | Test Implication |
|---|---|---|
| **P0 (Blocker)** | Violates five iron laws, data loss, transaction inconsistency, security breach, ARM64 deploy failure | Must have tests proving the invariant is preserved |
| **P1 (Important)** | Transaction boundary ambiguity, performance risk, maintainability degradation | Should have tests or explicit justification for absence |
| **P2 (Suggestion)** | UX polish, code tidiness, documentation gaps | Recorded but not blocking |

### Three-Agent Review Model

```
coding_agent  --> Implementation, unit tests, documentation
review_agent  --> Architecture review, security review, transaction review
test_agent    --> Test plans, contract tests, integration tests, gate acceptance
```

Test reviewers verify:
- All write APIs include `audit_events` + `events` in the same transaction
- Idempotency keys are correctly implemented
- Unified response envelope: success = `request_id` / `correlation_id` / `data` / `meta`; error = `request_id` / `correlation_id` / `error` (`code` / `message` / `details`)
- Sensitive fields (password, token, secret) never appear in logs or FTS indexes
- The test suite runs with a single command (`pytest`)

---

## Common Setup Issues

1. **"cannot commit transaction - SQL statements in progress"** (SQLite): This occurs when a previous SQL statement was not fully consumed (e.g., a `SELECT` with unread rows). The `db` fixture's `rollback()` before `close()` prevents this, but custom fixtures that manage their own sessions must follow the same pattern.

2. **FK constraint violations on `created_by_user_id`**: Many DAL tables reference `users.user_id`. Ensure the `test_user_id` autouse fixture is active (it is by default via conftest) or manually seed a user row before any writes.

3. **Missing `testcontainers` package**: When using `--postgres`, you need the `testcontainers` Python package. Install it with `pip install testcontainers` or `pip install -e ".[dev]"`.

4. **REDIS_URL connectivity**: The conftest sets `REDIS_URL=redis://localhost:6379/0` as a fallback. Tests that interact with Redis will fail if Redis is unavailable. For SQLite-only tests this is generally not an issue since Redis-dependent tests check connectivity first.

5. **psycopg2 ProgrammingError "can't adapt type 'dict'"**: When running against PostgreSQL with raw SQL, dict/list parameters must be adapted. The conftest registers psycopg2 adapters automatically when `DATABASE_URL` starts with `postgresql`.

---

## Next Steps

- See `doc/ARCHITECTURE.md` for a system-level overview of the components under test.
- See `doc/CONFIGURATION.md` for the environment variables referenced by the test suite.
- See `CLAUDE.md` for build commands, transaction patterns, and the project's five iron laws.
- See `REVIEW.md` for the full code review specification.
