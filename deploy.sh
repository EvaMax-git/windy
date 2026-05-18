#!/bin/bash
set -e

# ── Deploy Mneme3 API + Web ────────────────────────────────────────────
NAS=/mnt/nas/letta/Mneme3
TARGET="${DEPLOY_TARGET:-root@192.168.31.199}"

echo "=== Mneme3 部署 ==="

# Step 1: Sync backend code
echo "[1/4] Syncing backend code..."
rsync -a --exclude='__pycache__' --exclude='*.pyc' --exclude='node_modules' --exclude='.git' \
    "$NAS/mneme/" "$TARGET:/root/Mneme3/mneme/"
rsync -a "$NAS/pyproject.toml" "$TARGET:/root/Mneme3/"

# Step 2: Run DB migrations
echo "[2/4] Running DB migrations..."
ssh "$TARGET" "find /root/Mneme3 -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null; \
    cd /root/Mneme3/mneme/db/alembic && \
    DATABASE_URL="${DATABASE_URL:-postgresql://mneme:mneme123@localhost:5432/mneme}" \
    python3 -m alembic -c alembic.ini upgrade head 2>&1 | tail -1"

# Step 3: Build & sync frontend
echo "[3/4] Building frontend..."
cd "$NAS/mneme/web" && npx vite build 2>&1 | tail -1
rsync -a --delete "$NAS/mneme/web/dist/" "$TARGET:/var/www/mneme/"

# Step 4: Restart services & health check
echo "[4/4] Restarting services..."
ssh "$TARGET" "systemctl restart mneme-api && nginx -s reload && \
    sleep 4 && curl -s localhost:8000/health/ready && echo ' OK'"

echo "=== Done ==="
