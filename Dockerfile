# ═══════════════════════════════════════════════════════════════════
# Mneme3 — Multi-stage Dockerfile
# ═══════════════════════════════════════════════════════════════════
# Stage 1: builder — install dependencies into venv
# Stage 2: runtime — slim image with venv + app code
# ═══════════════════════════════════════════════════════════════════

# ── Stage 1: Builder ─────────────────────────────────────────────
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /build

# Use Chinese mirrors for faster downloads
RUN sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list.d/debian.sources

# Install build-time system deps
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy full source (needed for pip install .)
COPY pyproject.toml README.md ./
COPY mneme ./mneme

# Install Python dependencies into a virtual env
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com .


# ── Stage 2: Runtime ─────────────────────────────────────────────
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Use Chinese mirrors for faster downloads
RUN sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list.d/debian.sources

# Install runtime system deps (no build tools)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        postgresql-client \
        libgl1 \
        libglib2.0-0 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Copy virtual env from builder (pre-built dependencies)
COPY --from=builder /opt/venv /opt/venv

# Copy application code
COPY mneme ./mneme
COPY pyproject.toml README.md ./

# Non-root user
RUN useradd --create-home --no-log-init appuser

# Data directories (created as root, then chowned)
RUN mkdir -p /mneme_data/staging /mneme_data/keys /mneme_data/public /mneme_data/private /backups \
    && chown -R appuser:appuser /mneme_data /backups

# Declare mount points for persistent data
VOLUME ["/mneme_data", "/backups"]

# Expose API port
EXPOSE 8000

# Health check — curl the /health/ready endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8000/health/ready || exit 1

# Run as non-root
USER appuser

# Default command — API server
CMD ["uvicorn", "mneme.main:app", "--host", "0.0.0.0", "--port", "8000"]
