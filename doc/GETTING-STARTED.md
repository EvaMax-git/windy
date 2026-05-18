<!-- generated-by: gsd-doc-writer -->

# Getting Started

This guide walks you through setting up Mneme on your local machine, from prerequisites to running the full stack and executing tests.

## Prerequisites

| Component | Minimum Version | Notes |
|-----------|----------------|-------|
| Python | >= 3.10 | Required for the FastAPI backend and worker |
| Node.js | >= 20 | Required for the Vue 3 frontend (Vite 6) |
| npm | >= 10 | Ships with Node.js |
| PostgreSQL | 16 | With the `pgvector` extension enabled |
| Redis | 7 | Used for worker queue and dispatch |
| Docker + Docker Compose | -- | Optional; recommended for containerized setup |

> **Note on Node.js / npm versions:** The minimum versions (Node.js >= 20, npm >= 10) are not enforced by `package.json` (the file has no `engines` field). These are the versions used in CI and are recommended for local development. The CI workflow uses Node 20.

- **Python packages**: The project installs its dependencies via `pip install -e .` (see `pyproject.toml` for the full list, including FastAPI, SQLAlchemy 2.0, Alembic, Pydantic v2, psycopg2, Redis, and more).
- **PostgreSQL + pgvector**: The Docker Compose file uses the `pgvector/pgvector:pg16` image. If you run PostgreSQL manually, ensure the `pgvector` extension is installed.
- **Docker Compose**: Not required if you run PostgreSQL, Redis, the API, and the worker manually -- but it is the fastest path to a working environment.

## Installation Steps

### Option A: Docker Compose (recommended for first-time setup)

1.  **Clone the repository:**

    ```bash
    git clone <repo-url> Mneme3
    cd Mneme3
    ```

2.  **Copy the environment file:**

    ```bash
    cp .env.example .env
    ```

    The default values in `.env.example` are already tuned for the Docker Compose services. No edits are required for local startup.

3.  **Start the core services:**

    ```bash
    docker compose up -d postgres redis api worker
    ```

    This starts PostgreSQL (port 5432), Redis (port 6379), the Mneme API (port 8000), and the Mneme Worker.

4.  **Run database migrations inside the API container:**

    ```bash
    docker compose exec api alembic upgrade head
    ```

5.  **Verify the API is healthy:**

    ```bash
    curl http://localhost:8000/health/live
    ```

    Expected response: `{"status":"ok","environment":"local"}`

6.  **Start the observability stack (optional):**

    ```bash
    docker compose up -d prometheus grafana
    ```

    Grafana is available at `http://localhost:3000` (default credentials: `admin` / `mneme_grafana`).

### Option B: Manual Setup

1.  **Clone the repository and enter the project directory:**

    ```bash
    git clone <repo-url> Mneme3
    cd Mneme3
    ```

2.  **Copy and edit the environment file:**

    ```bash
    cp .env.example .env
    ```

    Edit `.env` and set connection strings pointing to your local PostgreSQL and Redis instances:

    ```ini
    DATABASE_URL=postgresql+psycopg2://mneme:your_password@localhost:5432/mneme
    REDIS_URL=redis://localhost:6379/0
    ```

3.  **Install the Python backend (development mode):**

    ```bash
    pip install -e .
    pip install -e ".[dev]"    # includes pytest and testcontainers
    ```

4.  **Run database migrations:**

    ```bash
    cd mneme/db/alembic
    alembic upgrade head
    cd ../../..
    ```

5.  **Install frontend dependencies:**

    ```bash
    cd mneme/web
    npm install
    cd ../..
    ```

6.  **Build the frontend production bundle (optional):**

    ```bash
    cd mneme/web && npm run build
    ```

    The API serves the built frontend from `mneme/web/dist/` or `mneme/static/` automatically.

## First Run

### Start the API server

```bash
uvicorn mneme.main:app --host 0.0.0.0 --port 8000
```

The API starts on `http://localhost:8000`. Open that URL in a browser to see the dashboard (if the frontend is built) or the service-info JSON.

API docs are available at `http://localhost:8000/docs` (Swagger UI).

### Start the Worker process

In a separate terminal:

```bash
python -m mneme.worker
```

The worker handles dispatched jobs (memory extraction, review, pipelines, decay, emotion inference, sublimation, spontaneous recall, etc.) through a Redis-backed lease/poll/retry pattern.

### Start the Frontend Dev Server

In a third terminal:

```bash
cd mneme/web
npm run dev
```

The Vite dev server starts on `http://localhost:5173`. API calls are proxied to the backend at `http://localhost:8000` automatically (see `mneme/web/vite.config.ts`).

### Verify the Full Stack

1. Check the API health: `curl http://localhost:8000/health/live`
2. Check the API readiness (DB + Redis): `curl http://localhost:8000/health/ready`
3. Open the frontend: `http://localhost:5173`
4. On first startup with an empty users table, the API seeds a bootstrap owner account configured via `MNEME_BOOTSTRAP_OWNER_USERNAME` / `MNEME_BOOTSTRAP_OWNER_EMAIL` / `MNEME_BOOTSTRAP_OWNER_PASSWORD` in `.env`.

## Running Tests

```bash
# Run the full test suite (58+ test files, 2,400+ test functions)
pytest

# Run a single test file
pytest tests/test_auth.py

# Run a single test function
pytest tests/test_auth.py -k test_login

# Verbose output, stop on first failure
pytest -x -v
```

Tests use `pytest >= 7.0` with `pytest-asyncio` and `testcontainers` (see `pyproject.toml` `[project.optional-dependencies] dev`).

## Common Setup Issues

### 1. Missing or misconfigured `.env` file

**Symptom:** `pydantic.ValidationError` or `Field required` on startup.

**Solution:** Copy `.env.example` to `.env` and ensure `DATABASE_URL` and `REDIS_URL` are set.

### 2. PostgreSQL connection refused

**Symptom:** `sqlalchemy.exc.OperationalError: could not connect to server`.

**Solutions:**
- If using Docker Compose, ensure the `postgres` service is running: `docker compose ps`.
- If running PostgreSQL manually, verify the host/port in `DATABASE_URL` match your local instance.
- Ensure the database exists and the user has access permissions.

### 3. Database migrations not applied

**Symptom:** API starts but endpoints return `relation does not exist` errors.

**Solution:** Run `alembic upgrade head` from `mneme/db/alembic/`. There are 28 migrations covering 71 tables (7 added by migration 0026 for the knowledge module redesign).

### 4. Port conflicts

**Symptom:** `Address already in use` when starting uvicorn or Vite.

**Default ports:**
- API: `8000` (change with the `--port` flag on uvicorn, or set `API_PORT` in `.env` for Docker)
- Frontend dev server: `5173` (change in `mneme/web/vite.config.ts` `server.port`)
- PostgreSQL: `5432`
- Redis: `6379`

### 5. CORS errors from the frontend dev server

**Symptom:** Browser console shows CORS errors when the Vite dev server calls the API.

**Solution:** The Vite config already proxies `/api` and `/health` requests to `http://localhost:8000`. Ensure the API is running on port 8000. If the API is on a different port, update the proxy target in `mneme/web/vite.config.ts`.

### 6. pgvector extension missing (manual PostgreSQL)

**Symptom:** Migration or query errors related to vector operators (`<->`, `<=>`).

**Solution:** Run `CREATE EXTENSION IF NOT EXISTS vector;` in your PostgreSQL database.

## Next Steps

- **Development workflow**: See `DEVELOPMENT.md` for build commands, code style, branch conventions, and PR process.
- **Running tests**: See `TESTING.md` for test framework details, coverage requirements, and CI integration.
- **Architecture**: See `doc/ARCHITECTURE.md` for the system overview, component diagram, and data flow.
- **Configuration**: See `doc/CONFIGURATION.md` for the full environment variable reference.
- **Agent instructions**: See `CLAUDE.md` for project build commands, conventions, and rules for AI assistants.
