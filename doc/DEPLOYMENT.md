<!-- generated-by: gsd-doc-writer -->

# Deployment

## Deployment targets

Mneme3 supports three deployment modes, from local development to production.

### Docker Compose (primary)

The recommended deployment method. `docker-compose.yaml` defines a full stack with core services and an optional observability stack.

**Core services (always deployed):**

| Service | Image | Port | Memory Limit |
|---------|-------|------|-------------|
| PostgreSQL (pgvector) | `pgvector/pgvector:pg16` | 5432 | 400 MB |
| Redis | `redis:7-alpine` | 6379 | 128 MB |
| Mneme API | built from `Dockerfile` | 8000 | 384 MB |
| Mneme Worker | built from `Dockerfile` | — | 384 MB |

**Observability stack (optional, for production monitoring):**

| Service | Image | Port | Memory Limit |
|---------|-------|------|-------------|
| Prometheus | `prom/prometheus:v2.53.0` | 9090 | 128 MB |
| Grafana | `grafana/grafana:11.1.0` | 3000 | 128 MB |
| postgres-exporter | `prometheuscommunity/postgres-exporter:v0.15.0` | 9187 | 32 MB |
| redis-exporter | `oliver006/redis_exporter:v1.65.0` | 9121 | 32 MB |
| node-exporter | `prom/node-exporter:v1.8.2` | 9100 | 32 MB |
| cAdvisor | `gcr.io/cadvisor/cadvisor:v0.49.1` | 8080 | 64 MB |

**Low-memory deployment (RAM <= 2 GB):** start only the four core services:

```bash
docker compose up -d postgres redis api worker
```
<!-- VERIFY: Docker Compose observability stack services are configured in docker-compose.yaml but their actual use is environment-dependent -->

**Full-stack startup:**

```bash
cp .env.example .env          # edit DATABASE_URL and REDIS_URL as needed
docker compose up -d          # starts all services
docker compose exec api alembic -c mneme/db/alembic/alembic.ini upgrade head   # run DB migrations inside container
# ⚠️  No built-in create-admin command exists.  Create the first admin user via
# direct DB insert (INSERT INTO users ...) or a custom bootstrap script.
# The auth API exposes only login/logout/me — there is no register endpoint.
```

### Manual deployment to 192.168.31.128 (bare-metal)

Target server runs nginx serving static files and proxying API requests to uvicorn.

**Architecture on 128:**

- nginx listens on port **5280**, serving static files from `/var/www/mneme/` and proxying `/api/*` to uvicorn on port **8111**
- uvicorn runs Mneme API (managed via systemd or nohup)
- Server account: `zyys` (password-based SSH, sudo via `echo <password> | sudo -S`)
<!-- VERIFY: nginx configuration on 192.168.31.128 (port 5280, proxy to 8111) is set up on the server and not versioned in this repository -->
<!-- VERIFY: SSH credentials for 192.168.31.128 (user zyys) are deployment-specific and managed outside the repository -->
<!-- SECURITY: deploy_to_128.py hardcodes PASSWORD='606808' on line 8. This credential is NOT managed outside the repository -- it is committed in plaintext. -->

**Deploy scripts for 128:**

| Script | Purpose |
|--------|---------|
| `deploy_to_128.py` | Full frontend deploy via paramiko SFTP: clean target, upload `dist/`, reload nginx |
| `deploy-frontend.ps1` | PowerShell alternative using SCP (fallback to rsync) |
| `_deploy.py` / `_deploy3.py` | Iterative deploy helpers for prototype development |

**Typical frontend deploy flow:**

```cmd
:: 1. Build frontend (from NAS, using UNC workaround)
net use X: \\192.168.31.28\zyys\letta\Mneme3\mneme\web /persistent:no
X:
node .\node_modules\vite\bin\vite.js build
C: && net use X: /delete

# 2. Deploy dist/ to server
python deploy_to_128.py

# 3. Verify
# Visit http://192.168.31.128:5280/app/knowledge-v2
```

**Backend deploy to 128** requires uploading Python files via SFTP and restarting uvicorn:

```bash
# Upload updated Python files to the server
scp -r mneme/ zyys@192.168.31.128:/home/zyys/Mneme3/mneme/

# Restart uvicorn on server
ssh zyys@192.168.31.128 "echo 606808 | sudo -S systemctl restart mneme-api"
```
<!-- VERIFY: Backend service management on 192.168.31.128 (systemd unit name mneme-api) is server-specific and not versioned in this repository -->

### Manual deployment to 192.168.31.199

Deployed via `deploy.sh` using rsync over SSH. Target: `root@192.168.31.199` (requires SSH key and rsync).

```bash
./deploy.sh
```

The script performs: (1) sync backend code via rsync, (2) run DB migrations, (3) build and sync frontend to `/var/www/mneme/`, (4) restart `mneme-api` via systemd and reload nginx.

> ⚠️ **Security note:** `deploy.sh` hardcodes a fallback `DATABASE_URL` containing the
> plaintext password `mneme:mneme123@localhost`. If the `DATABASE_URL` environment
> variable is not set, the script uses this default credential.

<!-- VERIFY: Target server 192.168.31.199 (user root) requires SSH public key authentication set up outside this repository -->

---

## Build pipeline

CI/CD runs via GitHub Actions in `.github/workflows/`.

### CI (`ci.yml`)

Triggered on **push to `main` and `release/**`** branches, and **pull requests to `main`**.

| Job | What it runs |
|-----|-------------|
| `lint` | Ruff import sorting check, format check, full lint on `mneme/` |
| `backend` | pytest full suite against PostgreSQL 16 + Redis 7 service containers |
| `frontend` | `vue-tsc --noEmit` type-check, then `npm run build` (artifact uploads `dist/`) |
| `docker` | Docker image build test (no push) using `build-push-action` with GHA cache |

**Note:** CI uses the plain `postgres:16` image (no pgvector extension), while the Docker Compose production deployment uses `pgvector/pgvector:pg16`. This means vector-related tests or features may behave differently between CI and production.

### CD (`cd.yml`)

Triggered on **push to `main`**, with concurrency (cancel in-progress). Only runs on `zyys/Mneme3` repository.

| Step | Action |
|------|--------|
| 1. Sync backend | rsync `mneme/` and `pyproject.toml` to `root@192.168.31.199:/root/Mneme3/` |
| 2. DB migrations | SSH: `alembic upgrade head` using `DATABASE_URL` secret |
| 3. Build & deploy frontend | `npm ci && npx vite build` (⚠️ known issue: CD workflow currently runs `npx ci` which should be `npm ci`), rsync `dist/` to `:/var/www/mneme/` |
| 4. Restart & health check | `systemctl restart mneme-api && nginx -s reload`, wait 4s, `curl localhost:8000/health/ready` |
<!-- KNOWN ISSUE: The CD workflow (cd.yml) does NOT reload nginx after frontend deploy,
     so static file changes may not be served until nginx is manually reloaded. -->
<!-- VERIFY: CD deploy target and DATABASE_URL secret are configured in GitHub repository settings and not visible in this repository -->

### Manual frontend build

For local or NAS-based builds that bypass CI:

```powershell
# On NAS (CMD doesn't support UNC paths)
net use X: \\192.168.31.28\zyys\letta\Mneme3\mneme\web /persistent:no
Set-Location X:
node .\node_modules\vite\bin\vite.js build     # use local vite binary, not npx
Set-Location C:; net use X: /delete

# Output: mneme/web/dist/
```

---

## Environment setup

All deployment targets require environment configuration. The canonical list is in `.env.example`. See `doc/CONFIGURATION.md` for the complete variable reference.

**Required for all deployments:**

| Variable | Purpose |
|----------|---------|
| `DATABASE_URL` | PostgreSQL connection string (`postgresql+psycopg2://...`) |
| `REDIS_URL` | Redis connection string (`redis://...`) |
| `MNEME_ENV` | Environment identifier (`local`, `staging`, `production`) |

**Frontend-specific:**

| Variable | Purpose |
|----------|---------|
| `VITE_LEGACY_REDIRECTS` | Feature flag: set to `"false"` to disable legacy route redirects (default: enabled) |

**Docker Compose:** Copy `.env.example` to `.env` and edit values. The `docker-compose.yaml` uses `${VAR:-default}` syntax with defaults matching `.env.example`.

**Bare-metal (128/199):** Set environment variables in the systemd unit file or in a `.env` file read by the application at startup. The application reads `DATABASE_URL` and `REDIS_URL` from the environment at runtime.

---

## Rollback procedure

### Database rollback

```bash
# Roll back the most recent migration
cd mneme/db/alembic
alembic -c alembic.ini downgrade -1

# To roll back multiple migrations, repeat or specify a target revision
alembic -c alembic.ini downgrade <revision>
```

### Docker Compose rollback

Redeploy the previous working Docker image tag:

```bash
# If using specific image tags
docker compose down
# Update image tag in docker-compose.yaml or .env
docker compose up -d
```

If no image tag was pinned, rebuild from the previous commit:

```bash
git checkout <previous-commit>
docker compose up -d --build api worker
```

### Bare-metal rollback (128)

Redeploy the previous frontend build from a backup or rebuild from the prior commit:

```bash
# Rebuild frontend from prior commit
git checkout <previous-commit>
cd mneme/web && npm run build
python deploy_to_128.py
```

For backend: stop the new uvicorn process, restore previous Python files from backup or git, and restart.

### Bare-metal rollback (199)

Redeploy using `deploy.sh` from the prior commit:

```bash
git checkout <previous-commit>
./deploy.sh
```

---

## Monitoring

### Health endpoints

The Mneme API exposes health-check endpoints at:

| Endpoint | Purpose |
|----------|---------|
| `GET /health/live` | Liveness probe — minimal check, responds if the process is running |
| `GET /health/startup` | Startup probe — returns healthy once all dependencies are connected |
| `GET /health/ready` | Readiness probe — checks database (required) and Redis (degraded if down) |
| `GET /health/features` | Runtime feature flags exposed to the frontend |
| `GET /metrics` | Prometheus metrics endpoint (scraped by Prometheus) |

Health endpoints are implemented in `mneme/api/routes/system/health.py`. All health paths shown above are relative to the `/api/v4` prefix (e.g. `/api/v4/health/ready`). The CD pipeline uses `/health/ready` as its health check after restart.

### Prometheus + Grafana stack

Included in `docker-compose.yaml` as the observability stack. Prometheus scrapes these targets:

| Job | Target | Metrics from |
|-----|--------|-------------|
| `mneme-api` | `api:8000` | Application-level metrics via `prometheus-client` (`>=0.20`) |
| `mneme-worker` | `worker:8001` | **NON-FUNCTIONAL** — the Worker runs `python -m mneme.worker` which is a pure job processor with no built-in HTTP server. Port 8001 is unreachable and will cause Prometheus scrape errors. Worker health is instead inferred from job processing metrics emitted indirectly. |
| `postgres` | `postgres-exporter:9187` | Database connection pool, query stats |
| `redis` | `redis-exporter:9121` | Memory usage, keyspace, hit rate |
| `node` | `node-exporter:9100` | Host CPU, memory, disk, network |
| `cadvisor` | `cadvisor:8080` | Per-container CPU, memory, I/O |

Config files: `config/prometheus/prometheus.yml` (scrape targets), `config/prometheus/alerts.yml` (alert rules), `config/grafana/dashboards/` and `config/grafana/provisioning/` (Grafana dashboards and data sources).

> ⚠️ **Known issue: Grafana dashboard mount shadowed.** `docker-compose.yaml` mounts both
> `./config/grafana/dashboards:/etc/grafana/provisioning/dashboards` and
> `./config/grafana/provisioning:/etc/grafana/provisioning`. The dashboards mount
> shadows the `dashboards/` subdirectory of the provisioning mount, so the Grafana
> container only sees the host `dashboards/` directory and not the `dashboards/`
> subdirectory from the `provisioning/` tree.

<!-- VERIFY: Grafana dashboard URLs and admin credentials (GRAFANA_USER / GRAFANA_PASSWORD) are configured in the deployment environment, not hardcoded in this repository -->

### Logging

All Docker Compose services use `json-file` logging driver with rotation: **10 MB per file, max 3 files**. The `config/logrotate/` directory contains host-level log rotation configuration for bare-metal deployments.

### Structured logging

The API emits JSON-structured logs with fields: `timestamp`, `level`, `request_id`, `actor_type`, `route`, `status_code`, `duration_ms`. These are consumed by the logging driver and can be forwarded to external log aggregation systems.
