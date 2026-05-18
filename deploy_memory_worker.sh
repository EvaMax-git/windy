#!/bin/bash
set -e

# ── Deploy Memory Auto-Extract Worker on port 199 ────────────────────────
# Requires passwordless SSH (key-based auth) to $DEPLOY_TARGET
NAS=/mnt/nas/letta/Mneme3
TARGET="${DEPLOY_TARGET:-root@192.168.31.199}"

echo "=== Mneme3 Memory Auto-Extract Worker Deploy ==="

# Step 1: Sync the new/modified files
echo "[1/4] Syncing code..."
rsync -a --exclude='__pycache__' --exclude='*.pyc' \
    "$NAS/mneme/worker/memory_auto_extract.py" \
    "$NAS/mneme/worker/memory_worker.py" \
    "$NAS/mneme/worker/__init__.py" \
    "$NAS/mneme/worker/app.py" \
    "$TARGET:/root/Mneme3/mneme/worker/"

rsync -a "$NAS/mneme/config.py" "$TARGET:/root/Mneme3/mneme/"

# Step 2: Install systemd service
echo "[2/4] Installing systemd service..."
ssh "$TARGET" 'cat > /etc/systemd/system/mneme-memory-worker.service << EOF
[Unit]
Description=Mneme Memory Auto-Extract Worker
After=network.target mneme-api.service
Requires=mneme-api.service

[Service]
Type=simple
User=root
WorkingDirectory=/root/Mneme3
EnvironmentFile=/root/Mneme3/.env
ExecStart=/usr/bin/python3 -m mneme.worker.memory_worker
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF'

# Step 3: Reload systemd and restart
echo "[3/4] Reloading systemd & restarting service..."
ssh "$TARGET" "systemctl daemon-reload && systemctl enable mneme-memory-worker && systemctl restart mneme-memory-worker"

# Step 4: Verify
echo "[4/4] Verifying..."
sleep 3
ssh "$TARGET" "curl -s http://localhost:199/health && echo '' && curl -s http://localhost:199/stats && echo ''"
echo ""
echo "=== Deploy complete ==="
echo "Verify manually:"
echo "  curl http://192.168.31.199:199/health"
echo "  curl http://192.168.31.199:199/stats"
echo "  journalctl -u mneme-memory-worker -f"
