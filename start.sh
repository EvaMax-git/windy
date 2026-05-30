#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
# Mneme3 — One-click startup script
# ═══════════════════════════════════════════════════════════════════
# Usage:
#   ./start.sh              # Start core services (postgres, redis, api, worker)
#   ./start.sh --all        # Start all services including observability
#   ./start.sh --watcher    # Start core + file watcher
#   ./start.sh --stop       # Stop all services
#   ./start.sh --status     # Show service status
# ═══════════════════════════════════════════════════════════════════

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log() { echo -e "${GREEN}[mneme]${NC} $*"; }
warn() { echo -e "${YELLOW}[mneme]${NC} $*"; }
err() { echo -e "${RED}[mneme]${NC} $*" >&2; }

# Check prerequisites
check_deps() {
    if ! command -v docker &>/dev/null; then
        err "Docker is not installed. Please install Docker first."
        exit 1
    fi
    if ! docker compose version &>/dev/null; then
        err "Docker Compose v2 is required. Please update Docker."
        exit 1
    fi
}

# Create .env from .env.example if missing
ensure_env() {
    if [ ! -f .env ]; then
        warn ".env not found, copying from .env.example"
        cp .env.example .env
        log "Created .env — please review and update secrets before production use."
    fi
}

# Create local data directories (for non-Docker development)
ensure_dirs() {
    mkdir -p mneme_data/{staging,keys,public,private,watch} backups
}

# Start core services
start_core() {
    log "Starting core services (postgres, redis, api, worker)..."
    docker compose up -d postgres redis api worker
    log "Core services started. API at http://localhost:${API_PORT:-8000}"
}

# Start all services including observability
start_all() {
    log "Starting all services..."
    docker compose up -d
    log "All services started."
    log "  API:        http://localhost:${API_PORT:-8000}"
    log "  Prometheus: http://localhost:${PROMETHEUS_PORT:-9090}"
    log "  Grafana:    http://localhost:${GRAFANA_PORT:-3000}"
}

# Start core + watcher
start_watcher() {
    log "Starting core services + file watcher..."
    docker compose up -d postgres redis api worker
    docker compose --profile watcher up -d watcher
    log "Watcher started. Drop files into mneme_data/watch/ for auto-import."
}

# Stop all services
stop_all() {
    log "Stopping all services..."
    docker compose down
    log "All services stopped."
}

# Show status
show_status() {
    docker compose ps -a
}

# Main
main() {
    check_deps
    ensure_env
    ensure_dirs

    case "${1:-}" in
        --all)
            start_all
            ;;
        --watcher)
            start_watcher
            ;;
        --stop)
            stop_all
            ;;
        --status)
            show_status
            ;;
        --help|-h)
            echo "Usage: $0 [--all|--watcher|--stop|--status]"
            echo ""
            echo "Options:"
            echo "  (none)       Start core services (postgres, redis, api, worker)"
            echo "  --all        Start all services including observability stack"
            echo "  --watcher    Start core services + file watcher"
            echo "  --stop       Stop all services"
            echo "  --status     Show service status"
            echo "  --help       Show this help"
            ;;
        "")
            start_core
            ;;
        *)
            err "Unknown option: $1"
            echo "Run '$0 --help' for usage."
            exit 1
            ;;
    esac
}

main "$@"
