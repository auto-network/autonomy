#!/usr/bin/env bash
# Start the dashboard server.
#
# Usage: ./tools/dashboard/start-dashboard.sh [--stop] [--status] [--restart]
#
# Config (env vars or defaults):
#   DASHBOARD_PORT   Port to listen on (default: 8080)
#   DASHBOARD_HOST   Host to bind to (default: 0.0.0.0)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(dirname "$(dirname "$SCRIPT_DIR")")"
PID_FILE="$REPO_ROOT/data/dashboard.pid"
LOG_FILE="$REPO_ROOT/data/dashboard.log"
VENV="$REPO_ROOT/.venv/bin/python"

PORT="${DASHBOARD_PORT:-8080}"
HOST="${DASHBOARD_HOST:-0.0.0.0}"

stop_server() {
    if [[ -f "$PID_FILE" ]]; then
        local pid
        pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            echo "Stopping dashboard (PID $pid)..."
            kill "$pid"
            for _ in $(seq 1 10); do
                kill -0 "$pid" 2>/dev/null || break
                sleep 0.5
            done
            if kill -0 "$pid" 2>/dev/null; then
                echo "WARN: Dashboard did not exit cleanly, sending SIGKILL"
                kill -9 "$pid" 2>/dev/null || true
            fi
            echo "Stopped."
        else
            echo "PID $pid not running (stale pid file)."
        fi
        rm -f "$PID_FILE"
    else
        # Try to find by port
        local port_pid
        port_pid=$(lsof -ti :"$PORT" 2>/dev/null || true)
        if [[ -n "$port_pid" ]]; then
            echo "Killing process on port $PORT (PID $port_pid)..."
            kill -9 "$port_pid" 2>/dev/null || true
            echo "Stopped."
        else
            echo "No dashboard process found"
        fi
    fi
}

show_status() {
    if [[ -f "$PID_FILE" ]]; then
        local pid
        pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            echo "dashboard running: PID $pid, port $PORT"
            return 0
        else
            echo "dashboard NOT running (stale pid file, PID $pid)"
            rm -f "$PID_FILE"
            return 1
        fi
    else
        echo "dashboard NOT running (no pid file)"
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
    --restart)
        stop_server
        sleep 1
        # fall through to start
        ;;
esac

# Stop existing if running
if [[ -f "$PID_FILE" ]]; then
    existing_pid=$(cat "$PID_FILE")
    if kill -0 "$existing_pid" 2>/dev/null; then
        echo "Dashboard already running (PID $existing_pid). Stopping first..."
        stop_server
        sleep 1
    else
        rm -f "$PID_FILE"
    fi
fi

# Start with --reload for hot reloading
mkdir -p "$(dirname "$LOG_FILE")"

echo "Starting dashboard: host=$HOST, port=$PORT (hot-reload enabled)"
nohup "$VENV" -m uvicorn tools.dashboard.server:app \
    --host "$HOST" \
    --port "$PORT" \
    --reload \
    --reload-dir tools/dashboard \
    >> "$LOG_FILE" 2>&1 &

DASHBOARD_PID=$!
echo "$DASHBOARD_PID" > "$PID_FILE"

sleep 2
if kill -0 "$DASHBOARD_PID" 2>/dev/null; then
    echo "dashboard started: PID $DASHBOARD_PID"
    echo "  URL: http://$HOST:$PORT"
    echo "  Log: $LOG_FILE"
    echo "  PID file: $PID_FILE"
    echo "  Hot-reload: tools/dashboard/"
else
    echo "ERROR: dashboard failed to start. Check $LOG_FILE" >&2
    rm -f "$PID_FILE"
    exit 1
fi
