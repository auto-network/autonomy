#!/usr/bin/env bash
# Start the dolt SQL server independently of bd.
#
# Runs dolt sql-server on the fixed port from .beads/config.yaml (default 3306)
# via nohup so it survives shell exit. Prevents bd from auto-starting its own
# dolt instance (which corrupts shared .beads/ state when agents mount it r/w).
#
# Usage: ./agents/start-dolt.sh [--stop] [--status]
#
# Prerequisites: dolt binary in PATH (~/go/bin/dolt)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
BEADS_DIR="${BEADS_DIR:-$REPO_ROOT/.beads}"

# Read fixed port from config.yaml (grep for dolt-server-port)
PORT=$(grep -oP 'dolt-server-port:\s*\K[0-9]+' "$BEADS_DIR/config.yaml" 2>/dev/null || echo "3306")
PID_FILE="$BEADS_DIR/dolt-server.pid"
LOG_FILE="$REPO_ROOT/data/dolt-server.log"

# Ensure dolt is on PATH
export PATH="$HOME/go/bin:$PATH"

stop_server() {
    if [[ -f "$PID_FILE" ]]; then
        local pid
        pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            echo "Stopping dolt server (PID $pid)..."
            kill "$pid"
            # Wait up to 5s for clean shutdown
            for _ in $(seq 1 10); do
                kill -0 "$pid" 2>/dev/null || break
                sleep 0.5
            done
            if kill -0 "$pid" 2>/dev/null; then
                echo "WARN: Dolt did not exit cleanly, sending SIGKILL"
                kill -9 "$pid" 2>/dev/null || true
            fi
            echo "Stopped."
        else
            echo "PID $pid not running (stale pid file)."
        fi
        rm -f "$PID_FILE"
    else
        echo "No pid file found at $PID_FILE"
    fi
}

show_status() {
    if [[ -f "$PID_FILE" ]]; then
        local pid
        pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            echo "dolt server running: PID $pid, port $PORT"
            return 0
        else
            echo "dolt server NOT running (stale pid file, PID $pid)"
            rm -f "$PID_FILE"
            return 1
        fi
    else
        echo "dolt server NOT running (no pid file)"
        return 1
    fi
}

case "${1:-}" in
    --stop)
        stop_server
        exit 0
        ;;
    --status)
        show_status
        exit $?
        ;;
esac

# ── Preflight checks ─────────────────────────────────
if ! command -v dolt &>/dev/null; then
    echo "ERROR: dolt not found in PATH" >&2
    echo "Install: go install github.com/dolthub/dolt/go/cmd/dolt@latest" >&2
    exit 1
fi

# Stop existing server if running (avoids port conflicts)
if [[ -f "$PID_FILE" ]]; then
    existing_pid=$(cat "$PID_FILE")
    if kill -0 "$existing_pid" 2>/dev/null; then
        echo "Dolt already running (PID $existing_pid). Stopping first..."
        stop_server
    else
        rm -f "$PID_FILE"
    fi
fi

# ── Initialize dolt repo if needed ───────────────────
if [[ ! -d "$BEADS_DIR/.dolt" ]]; then
    echo "Initializing dolt repository in $BEADS_DIR..."
    (cd "$BEADS_DIR" && dolt init --name autonomy --email agent@autonomy.local 2>/dev/null) || true
fi

# ── Start server ─────────────────────────────────────
mkdir -p "$(dirname "$LOG_FILE")"

echo "Starting dolt sql-server on port $PORT..."
nohup dolt sql-server \
    --host 0.0.0.0 \
    --port "$PORT" \
    --data-dir "$BEADS_DIR" \
    >> "$LOG_FILE" 2>&1 &

DOLT_PID=$!
echo "$DOLT_PID" > "$PID_FILE"

# Verify it started
sleep 1
if kill -0 "$DOLT_PID" 2>/dev/null; then
    echo "dolt server started: PID $DOLT_PID, port $PORT"
    echo "  Log: $LOG_FILE"
    echo "  PID file: $PID_FILE"
else
    echo "ERROR: dolt server failed to start. Check $LOG_FILE" >&2
    rm -f "$PID_FILE"
    exit 1
fi
