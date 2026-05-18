<!-- generated-by: gsd-doc-writer -->

# Configuration

Mneme3 uses **pydantic-settings** (`mneme/config.py`) as the single source of truth for all application configuration. All settings are loaded from environment variables, with a `.env` file in the project root as the primary override mechanism. The Docker Compose file (`docker-compose.yaml`) provides defaults for containerized environments.

---

## Environment Variables

All backend environment variables are consumed by `mneme/config.py` via `pydantic-settings`. The configuration class uses `env_file=".env"` and `extra="ignore"` -- unknown variables are silently ignored.

### Core (Required)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DATABASE_URL` | **Yes** | -- | PostgreSQL connection string. Format: `postgresql+psycopg2://user:pass@host:port/db` |
| `REDIS_URL` | **Yes** | -- | Redis connection string. Format: `redis://host:port/db` |

### Server Identity

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MNEME_ENV` | No | `local` | Deployment environment label (`local`, `staging`, `production`) |
| `MNEME_LOG_LEVEL` | No | `INFO` | Root logger level (`DEBUG`, `INFO`, `WARNING`, `ERROR`). Third-party loggers (uvicorn, SQLAlchemy) are kept at `WARNING` regardless. |

### Session and CORS

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MNEME_SESSION_TTL_HOURS` | No | `24` | Session cookie lifetime in hours (minimum: 1) |
| `MNEME_SESSION_COOKIE_NAME` | No | `mneme_session` | Name of the session cookie |
| `MNEME_SESSION_COOKIE_SECURE` | No | `false` | Set `Secure` flag on session cookie (should be `true` in production). **Note: `.env.example` sets this to `true`, overriding the code default of `false`.** |
| `MNEME_FRONTEND_ORIGINS` | No | `http://localhost:5173,http://localhost:5174,http://127.0.0.1:5173,http://127.0.0.1:5174,http://192.168.31.87:5173,http://192.168.31.87:5174` | Comma-separated list of allowed CORS origins |

### Bootstrap Owner

When the users table is empty at startup, the API automatically creates an owner account if configured:

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MNEME_BOOTSTRAP_OWNER_USERNAME` | No | `owner` | Username for initial owner account |
| `MNEME_BOOTSTRAP_OWNER_EMAIL` | No | `null` | Email for initial owner account |
| `MNEME_BOOTSTRAP_OWNER_PASSWORD` | No | `null` | Password for initial owner account. If not set, no bootstrap account is created. |

### Worker / Lease

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MNEME_WORKER_LEASE_TTL_SECONDS` | No | `30` | Lease TTL in seconds (min: 5) |
| `MNEME_WORKER_LEASE_HEARTBEAT_INTERVAL_SECONDS` | No | `10` | Heartbeat interval in seconds (min: 1) |
| `MNEME_WORKER_LEASE_NAME` | No | `dispatcher` | Lease name for worker instance identification |

### Worker / Retry Sweeper

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MNEME_WORKER_RETRY_BASE_DELAY_SECONDS` | No | `5` | Base backoff delay for retries (min: 1) |
| `MNEME_WORKER_RETRY_MAX_DELAY_SECONDS` | No | `3600` | Maximum backoff ceiling in seconds (min: 1) |
| `MNEME_WORKER_RETRY_MAX_ATTEMPTS` | No | `5` | Maximum dispatch attempts before promotion to dead_letters (min: 1) |
| `MNEME_WORKER_RETRY_SWEEPER_INTERVAL_SECONDS` | No | `10` | Interval between retry sweeper scan cycles (min: 1) |

### Worker / Recovery Sweeper

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MNEME_WORKER_RECOVERY_SWEEPER_INTERVAL_SECONDS` | No | `30` | Interval between recovery sweeper scan cycles (min: 1) |
| `MNEME_WORKER_DISPATCHING_TIMEOUT_SECONDS` | No | `120` | Seconds after which a 'dispatching' event is considered stuck (min: 10) |

### Worker / Review Timeout Checker

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MNEME_WORKER_REVIEW_TIMEOUT_CHECK_INTERVAL_SECONDS` | No | `60` | Interval between review timeout check cycles (min: 10) |

### Worker / Spontaneous Recall

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MNEME_WORKER_SPONTANEOUS_RECALL_ENABLED` | No | `true` | Enable the spontaneous recall sweeper (scans for memory contradictions) |
| `MNEME_WORKER_SPONTANEOUS_RECALL_INTERVAL_SECONDS` | No | `300` | Interval between recall scan cycles (min: 30) |
| `MNEME_WORKER_SPONTANEOUS_RECALL_MIN_CONFIDENCE` | No | `0.65` | Minimum LLM confidence to create conflict alerts (0.0-1.0) |
| `MNEME_WORKER_SPONTANEOUS_RECALL_MAX_PAIRS` | No | `20` | Maximum conflict candidate pairs to evaluate per sweep (1-200) |

### Worker / Memory Sublimation

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MNEME_WORKER_SUBLIMATION_ENABLED` | No | `true` | Enable the memory sublimation sweeper (abstracts similar events into consensus) |
| `MNEME_WORKER_SUBLIMATION_INTERVAL_SECONDS` | No | `600` | Interval between sublimation scan cycles (min: 60) |
| `MNEME_WORKER_SUBLIMATION_MIN_CLUSTER_SIZE` | No | `5` | Minimum similar memories to trigger sublimation (min: 2) |
| `MNEME_WORKER_SUBLIMATION_MIN_SIMILARITY` | No | `0.80` | Minimum cosine similarity for clustering (0.0-1.0) |
| `MNEME_WORKER_SUBLIMATION_MAX_CLUSTERS` | No | `10` | Maximum clusters to evaluate per sweep (1-50) |

### Worker / Memory Time Decay

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MNEME_WORKER_MEMORY_DECAY_ENABLED` | No | `true` | Enable periodic memory time-decay sweeper |
| `MNEME_WORKER_MEMORY_DECAY_INTERVAL_SECONDS` | No | `300` | Interval between decay sweeper cycles (min: 10) |
| `MNEME_DECAY_RATE_PER_DAY` | No | `0.05` | Daily linear decay rate (0.0-1.0, 0.05 = 5% per day) |
| `MNEME_DECAY_ACTIVE_THRESHOLD` | No | `0.7` | decay_score >= this -> `active` state (ge=0.0, le=1.0) |
| `MNEME_DECAY_SILENT_THRESHOLD` | No | `0.3` | decay_score between this and `MNEME_DECAY_ACTIVE_THRESHOLD` (0.7) -> `decaying` state (ge=0.0, le=1.0) |
| `MNEME_DECAY_ARCHIVE_THRESHOLD` | No | `0.1` | decay_score < this -> `archived` state (ge=0.0, le=1.0) |
| `MNEME_DECAY_REINFORCEMENT_BONUS` | No | `0.15` | Bonus added to decay_score on access/reinforcement (0.0-1.0) |
| `MNEME_DECAY_MAX_BATCH_SIZE` | No | `500` | Maximum memories per decay sweeper batch (min: 1) |

### Worker / Emotion Inference

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MNEME_WORKER_EMOTION_INFER_ENABLED` | No | `true` | Enable periodic emotion inference sweeper |
| `MNEME_WORKER_EMOTION_INFER_INTERVAL_SECONDS` | No | `600` | Interval between emotion inference sweeper cycles (min: 60) |
| `MNEME_EMOTION_INFER_BATCH_SIZE` | No | `200` | Maximum memories per inference batch (min: 1) |
| `MNEME_EMOTION_MIN_SIGNAL_THRESHOLD` | No | `0.5` | Minimum total signal strength for non-neutral classification (ge=0.0) |
| `MNEME_EMOTION_STRONG_SIGNAL_THRESHOLD` | No | `5.0` | Signal strength at which uncertainty approaches 0 (ge=0.0) |
| `MNEME_EMOTION_REINFER_UNCERTAINTY_THRESHOLD` | No | `0.6` | Re-infer emotion if uncertainty_score is above this (0.0-1.0) |

### Gateway

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MNEME_GATEWAY_CALL_TIMEOUT_SECONDS` | No | `120` | Default HTTP timeout for Gateway provider calls (min: 5) |
| `MNEME_GATEWAY_MAX_RETRIES` | No | `1` | Maximum automatic retries for Gateway calls (0-5) |
| `MNEME_GATEWAY_RETRY_BACKOFF_BASE_SECONDS` | No | `1.0` | Base backoff seconds between Gateway retries (min: 0.1) |

### Vault / Envelope Encryption

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MNEME_VAULT_KEK` | No | `""` (auto-generated) | Base64-encoded 256-bit Key Encryption Key. If empty, a random key is generated at startup. **Must be set to a persistent value in production.** |
| `MNEME_VAULT_KEY_VERSION` | No | `v1` | Default key version string for new credentials |

### Backup / Restore

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MNEME_BACKUP_ROOT` | No | `""` (falls back to `MnemeData/backups` relative to CWD) | Root directory for backup output. **Docker Compose overrides this to `/backups` via `MNEME_BACKUP_ROOT=${MNEME_BACKUP_ROOT:-/backups}`.** |

### Storage

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MNEME_STORAGE_ROOT` | No | `mneme_data` | Root directory for file storage (staging + assets) |
| `MNEME_STAGING_SUBDIR` | No | `staging` | Subdirectory under `MNEME_STORAGE_ROOT` for staging files |
| `MNEME_MAX_UPLOAD_SIZE_BYTES` | No | `104857600` (100 MB) | Maximum allowed file upload size in bytes (min: 1) |
| `MNEME_ALLOWED_MIME_TYPES` | No | (comprehensive list -- see `config.py`) | Comma-separated list of allowed MIME types for upload. Covers text, JSON, XML, PDF, Office documents, images, audio, video, and archives. |
| `MNEME_STORAGE_BACKEND` | No | `mneme_data` | Storage backend identifier (currently only `mneme_data` supported) |

### Context Assembly

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MNEME_CONTEXT_ASSEMBLY_MAX_TOKENS` | No | `128000` | Default max tokens for context assembly (min: 512) |
| `MNEME_CONTEXT_ASSEMBLY_OUTPUT_RESERVE` | No | `4096` | Tokens reserved for model output (min: 256) |
| `MNEME_CONTEXT_ASSEMBLY_SYSTEM_OVERHEAD` | No | `2048` | Tokens reserved for system prompt overhead (min: 0) |
| `MNEME_CONTEXT_ASSEMBLY_ALWAYS_RATIO` | No | `0.50` | Fraction of usable budget for 'always' cards (0.0-1.0) |
| `MNEME_CONTEXT_ASSEMBLY_MODERATE_RATIO` | No | `0.30` | Fraction of usable budget for 'moderate' cards (0.0-1.0) |
| `MNEME_CONTEXT_ASSEMBLY_ON_DEMAND_RATIO` | No | `0.20` | Fraction of usable budget for 'on_demand' cards (0.0-1.0) |

### PPR Graph Search

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MNEME_PPR_SEARCH_ENABLED` | No | `true` | Enable PPR graph traversal for global search recall |
| `MNEME_PPR_TELEPORT_ALPHA` | No | `0.85` | PPR teleport probability (0.0-1.0, higher = more graph exploration) |
| `MNEME_PPR_MAX_SEEDS` | No | `8` | Maximum seed nodes for PPR traversal (1-50) |
| `MNEME_PPR_TOP_K` | No | `12` | Maximum PPR-discovered nodes to return (1-100) |

### Temporal Cluster Search

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MNEME_TEMPORAL_CLUSTER_ENABLED` | No | `true` | Enable temporal shape clustering for fuzzy time queries |
| `MNEME_TEMPORAL_CLUSTER_TOP_K` | No | `8` | Maximum temporally-clustered memories to return (1-50) |

### Feature Flags

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MNEME_FEATURE_LEGACY_REDIRECTS` | No | `true` | Enable legacy URL redirects on the API side (30-day grace period). Set to `false` to immediately return 404 for old routes. |
| `VITE_LEGACY_REDIRECTS` | No | `true` | Frontend compile-time feature flag. When `false`, Vue Router skips registering legacy redirect routes. Set via `mneme/web/.env.production`. **Note: this file does not exist in the repository — users must create it.** |

### Docker Compose (Container-Specific)

These variables are only used by `docker-compose.yaml` and are **not** consumed by the Python application:

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `POSTGRES_DB` | No | `mneme` | PostgreSQL database name |
| `POSTGRES_USER` | No | `mneme` | PostgreSQL user |
| `POSTGRES_PASSWORD` | No | `mneme_dev_password` | PostgreSQL password |
| `POSTGRES_PORT` | No | `5432` | PostgreSQL port |
| `POSTGRES_HOST` | No | `postgres` | **Orphan variable** -- defined in `.env.example` but not consumed by config.py, docker-compose.yaml, or any application code. |
| `REDIS_PORT` | No | `6379` | Redis host port (only affects host port mapping `"${REDIS_PORT:-6379}:6379"`; internal Redis connection uses `REDIS_URL`) |
| `API_PORT` | No | `8000` | API service host port |

### Observability Stack (Docker Compose)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `PROMETHEUS_RETENTION` | No | `30d` | Prometheus TSDB retention period |
| `PROMETHEUS_PORT` | No | `9090` | Prometheus host port |
| `GRAFANA_USER` | No | `admin` | Grafana admin username |
| `GRAFANA_PASSWORD` | No | `mneme_grafana` | Grafana admin password |
| `GRAFANA_PORT` | No | `3000` | Grafana host port |
| `GRAFANA_ROOT_URL` | No | `http://localhost:3000` | Grafana root URL for redirects |

---

## Config File Format

### `.env` File

The `.env` file lives at the project root and uses standard `KEY=value` format. Copy `.env.example` to get started:

```bash
cp .env.example .env
```

Minimal production example:

```env
MNEME_ENV=production
MNEME_LOG_LEVEL=WARNING
DATABASE_URL=postgresql+psycopg2://user:password@db-host:5432/mneme
REDIS_URL=redis://redis-host:6379/0
MNEME_SESSION_COOKIE_SECURE=true
MNEME_VAULT_KEK=<base64-encoded-256-bit-key>
```

### Frontend Vite Config (`mneme/web/vite.config.ts`)

The Vite dev server is configured to:

- Bind on `0.0.0.0:5173`
- Use `@vitejs/plugin-vue` for Vue 3 SFC compilation
- Set up a `@` alias pointing to `src/`
- Proxy `/api` and `/health` requests to `http://localhost:8000`

In production, the frontend is built to `mneme/web/dist/` and served as static files by the FastAPI application via `StaticFiles` mount.

### Prometheus Config (`config/prometheus/prometheus.yml`)

Scrape interval: 15s. Targets: `mneme-api` (port 8000), `mneme-worker` (port 8001), `postgres-exporter`, `redis-exporter`, `node-exporter`, `cAdvisor`, and self-monitoring.

---

## Required vs Optional Settings

### Strictly Required (startup fails if missing)

| Setting | Validation |
|---------|-----------|
| `DATABASE_URL` | `pydantic-settings` raises `ValidationError` if unset. No default value. |
| `REDIS_URL` | `pydantic-settings` raises `ValidationError` if unset. No default value. |

These are the only two settings without defaults. All other settings have sensible defaults and the application will start without them.

### Recommended for Production

| Setting | Reason |
|---------|--------|
| `MNEME_VAULT_KEK` | Without a persistent KEK, credentials encrypted with auto-generated keys are lost on restart. |
| `MNEME_SESSION_COOKIE_SECURE` | Must be `true` when using HTTPS. |
| `MNEME_ENV` | Set to `production` for clarity. |
| `MNEME_LOG_LEVEL` | Use `WARNING` or `ERROR` to reduce log volume. |
| `MNEME_BOOTSTRAP_OWNER_PASSWORD` | Set a strong password for the initial owner account. |

---

## Defaults

All defaults are defined in `mneme/config.py` as `Field(default=...)` values on the `Settings` class. A comprehensive list:

| Category | Setting | Default Value |
|----------|---------|---------------|
| Environment | `MNEME_ENV` | `local` |
| Logging | `MNEME_LOG_LEVEL` | `INFO` |
| Session | `MNEME_SESSION_TTL_HOURS` | `24` |
| Session | `MNEME_SESSION_COOKIE_NAME` | `mneme_session` |
| Session | `MNEME_SESSION_COOKIE_SECURE` | `false` |
| CORS | `MNEME_FRONTEND_ORIGINS` | 6 localhost/127.0.0.1/192.168.31.87 origins |
| Bootstrap | `MNEME_BOOTSTRAP_OWNER_USERNAME` | `owner` |
| Bootstrap | `MNEME_BOOTSTRAP_OWNER_EMAIL` | `null` |
| Bootstrap | `MNEME_BOOTSTRAP_OWNER_PASSWORD` | `null` |
| Worker | `MNEME_WORKER_LEASE_TTL_SECONDS` | `30` |
| Worker | `MNEME_WORKER_LEASE_HEARTBEAT_INTERVAL_SECONDS` | `10` |
| Worker | `MNEME_WORKER_LEASE_NAME` | `dispatcher` |
| Worker | `MNEME_WORKER_RETRY_BASE_DELAY_SECONDS` | `5` |
| Worker | `MNEME_WORKER_RETRY_MAX_DELAY_SECONDS` | `3600` |
| Worker | `MNEME_WORKER_RETRY_MAX_ATTEMPTS` | `5` |
| Worker | `MNEME_WORKER_RETRY_SWEEPER_INTERVAL_SECONDS` | `10` |
| Worker | `MNEME_WORKER_RECOVERY_SWEEPER_INTERVAL_SECONDS` | `30` |
| Worker | `MNEME_WORKER_DISPATCHING_TIMEOUT_SECONDS` | `120` |
| Worker | `MNEME_WORKER_REVIEW_TIMEOUT_CHECK_INTERVAL_SECONDS` | `60` |
| Worker | `MNEME_WORKER_SPONTANEOUS_RECALL_ENABLED` | `true` |
| Worker | `MNEME_WORKER_SPONTANEOUS_RECALL_INTERVAL_SECONDS` | `300` |
| Worker | `MNEME_WORKER_SPONTANEOUS_RECALL_MIN_CONFIDENCE` | `0.65` |
| Worker | `MNEME_WORKER_SPONTANEOUS_RECALL_MAX_PAIRS` | `20` |
| Worker | `MNEME_WORKER_SUBLIMATION_ENABLED` | `true` |
| Worker | `MNEME_WORKER_SUBLIMATION_INTERVAL_SECONDS` | `600` |
| Worker | `MNEME_WORKER_SUBLIMATION_MIN_CLUSTER_SIZE` | `5` |
| Worker | `MNEME_WORKER_SUBLIMATION_MIN_SIMILARITY` | `0.80` |
| Worker | `MNEME_WORKER_SUBLIMATION_MAX_CLUSTERS` | `10` |
| Worker | `MNEME_WORKER_MEMORY_DECAY_ENABLED` | `true` |
| Worker | `MNEME_WORKER_MEMORY_DECAY_INTERVAL_SECONDS` | `300` |
| Memory | `MNEME_DECAY_RATE_PER_DAY` | `0.05` |
| Memory | `MNEME_DECAY_ACTIVE_THRESHOLD` | `0.7` |
| Memory | `MNEME_DECAY_SILENT_THRESHOLD` | `0.3` |
| Memory | `MNEME_DECAY_ARCHIVE_THRESHOLD` | `0.1` |
| Memory | `MNEME_DECAY_REINFORCEMENT_BONUS` | `0.15` |
| Memory | `MNEME_DECAY_MAX_BATCH_SIZE` | `500` |
| Memory | `MNEME_WORKER_EMOTION_INFER_ENABLED` | `true` |
| Memory | `MNEME_WORKER_EMOTION_INFER_INTERVAL_SECONDS` | `600` |
| Memory | `MNEME_EMOTION_INFER_BATCH_SIZE` | `200` |
| Memory | `MNEME_EMOTION_MIN_SIGNAL_THRESHOLD` | `0.5` |
| Memory | `MNEME_EMOTION_STRONG_SIGNAL_THRESHOLD` | `5.0` |
| Memory | `MNEME_EMOTION_REINFER_UNCERTAINTY_THRESHOLD` | `0.6` |
| Gateway | `MNEME_GATEWAY_CALL_TIMEOUT_SECONDS` | `120` |
| Gateway | `MNEME_GATEWAY_MAX_RETRIES` | `1` |
| Gateway | `MNEME_GATEWAY_RETRY_BACKOFF_BASE_SECONDS` | `1.0` |
| Vault | `MNEME_VAULT_KEK` | `""` (auto-generate) |
| Vault | `MNEME_VAULT_KEY_VERSION` | `v1` |
| Backup | `MNEME_BACKUP_ROOT` | `""` (CWD/MnemeData/backups; Docker override: `/backups`) |
| Storage | `MNEME_STORAGE_ROOT` | `mneme_data` |
| Storage | `MNEME_STAGING_SUBDIR` | `staging` |
| Storage | `MNEME_MAX_UPLOAD_SIZE_BYTES` | `104857600` |
| Storage | `MNEME_ALLOWED_MIME_TYPES` | `text/plain,text/csv,text/markdown,text/html,text/x-python,application/json,application/xml,application/pdf,application/msword,application/vnd.openxmlformats-officedocument.wordprocessingml.document,application/vnd.ms-excel,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,application/vnd.ms-powerpoint,application/vnd.openxmlformats-officedocument.presentationml.presentation,image/png,image/jpeg,image/gif,image/webp,image/svg+xml,audio/mpeg,audio/wav,audio/ogg,audio/flac,video/mp4,video/webm,application/zip,application/gzip,application/x-tar` |
| Storage | `MNEME_STORAGE_BACKEND` | `mneme_data` |
| Context | `MNEME_CONTEXT_ASSEMBLY_MAX_TOKENS` | `128000` |
| Context | `MNEME_CONTEXT_ASSEMBLY_OUTPUT_RESERVE` | `4096` |
| Context | `MNEME_CONTEXT_ASSEMBLY_SYSTEM_OVERHEAD` | `2048` |
| Context | `MNEME_CONTEXT_ASSEMBLY_ALWAYS_RATIO` | `0.50` |
| Context | `MNEME_CONTEXT_ASSEMBLY_MODERATE_RATIO` | `0.30` |
| Context | `MNEME_CONTEXT_ASSEMBLY_ON_DEMAND_RATIO` | `0.20` |
| Graph | `MNEME_PPR_SEARCH_ENABLED` | `true` |
| Graph | `MNEME_PPR_TELEPORT_ALPHA` | `0.85` |
| Graph | `MNEME_PPR_MAX_SEEDS` | `8` |
| Graph | `MNEME_PPR_TOP_K` | `12` |
| Graph | `MNEME_TEMPORAL_CLUSTER_ENABLED` | `true` |
| Graph | `MNEME_TEMPORAL_CLUSTER_TOP_K` | `8` |
| Feature | `MNEME_FEATURE_LEGACY_REDIRECTS` | `true` |

---

## Per-Environment Overrides

### Development (Local)

Copy `.env.example` and keep defaults. The Vite dev server proxies `/api` and `/health` to `http://localhost:8000`:

```bash
cp .env.example .env
pip install -e ".[dev]"
uvicorn mneme.main:app --host 0.0.0.0 --port 8000
# In separate terminal:
cd mneme/web && npm run dev   # starts on :5173, proxies to :8000
```

CORS defaults allow `localhost:5173` and `localhost:5174` origins for frontend dev with hot reload.

### Docker Compose (Local Containerized)

```bash
docker compose up -d postgres redis api worker
docker compose exec api alembic upgrade head
docker compose exec api python -m mneme.db.admin_queries create-admin
```

Docker Compose sets `DATABASE_URL` and `REDIS_URL` using service names (`postgres`, `redis`) instead of localhost. The `docker-compose.yaml` file constructs these from individual `POSTGRES_*` and `REDIS_URL` variables.

Low-memory deployment (RAM <= 2GB): only start the 4 core services:

```bash
docker compose up -d postgres redis api worker
```

### Production

No per-environment `.env` files exist in the repository. For production:

1. Set `MNEME_ENV=production`
2. Set `MNEME_SESSION_COOKIE_SECURE=true`
3. Set `MNEME_VAULT_KEK` to a persistent, securely-stored 256-bit key
4. Tighten `MNEME_FRONTEND_ORIGINS` to the specific production frontend origin
5. Set `MNEME_LOG_LEVEL=WARNING` to reduce log noise
6. Set a strong `MNEME_BOOTSTRAP_OWNER_PASSWORD` for the initial admin account
7. Set provider API keys via the Gateway UI or vault credentials -- there are no hardcoded provider env vars

### Frontend Feature Flag: Legacy Redirects

The frontend checks `VITE_LEGACY_REDIRECTS` at build time (via `import.meta.env.VITE_LEGACY_REDIRECTS`). When `false`, old-route redirects are excluded from the production bundle entirely. Set this in `mneme/web/.env.production` (note: this file does not exist in the repository — create it if needed):

```env
VITE_LEGACY_REDIRECTS=false
```

The API-side equivalent is `MNEME_FEATURE_LEGACY_REDIRECTS`. Both flags default to `true` with a 30-day grace period expiring on 2026-06-04. After this date, the legacy redirect routes in `src/router/index.ts` stop functioning regardless of the flag via a runtime date check.

### Observability Stack

The full observability stack (Prometheus, Grafana, postgres-exporter, redis-exporter, node-exporter, cAdvisor) is configured in `docker-compose.yaml` but can be started independently:

```bash
docker compose up -d prometheus grafana
```

Grafana dashboards are provisioned from `config/grafana/dashboards/`. Prometheus rules are loaded from `config/prometheus/alerts.yml`.

<!-- VERIFY: Grafana dashboards at config/grafana/dashboards/ -->

---

## Configuration Loading Flow

1. `pydantic-settings` loads `.env` from the current working directory (CWD), not the project root. Ensure commands are run from the project root where `.env` resides, or use an absolute path.
2. Individual `os.environ` values override `.env` values
3. `get_settings()` (cached via `@lru_cache`) returns the singleton `Settings` instance
4. `main.py` `create_app()` calls `get_settings()` on startup and configures logging, CORS, and middleware
5. Feature flags are exposed via `GET /health/features` endpoint
