#!/usr/bin/env bash
# Start the dashboard server.
#
# Usage: ./tools/dashboard/start-dashboard.sh [--stop] [--status] [--restart]
#
# Config (env vars or defaults):
#   DASHBOARD_PORT   Port to listen on (default: 8080)
#   DASHBOARD_HOST   Host to bind to (default: 0.0.0.0)
#
# Process management:
#   Starts tailwindcss --watch and uvicorn in a shared process group (via setsid).
#   The PGID is stored in the PID file; stop kills the entire group in one shot.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(dirname "$(dirname "$SCRIPT_DIR")")"
PID_FILE="$REPO_ROOT/data/dashboard.pid"
LOG_FILE="$REPO_ROOT/data/dashboard.log"
VENV="$REPO_ROOT/.venv/bin/python"

TAILWIND_BIN="$SCRIPT_DIR/tailwindcss"
CSS_INPUT="$SCRIPT_DIR/tailwind.input.css"
CSS_OUTPUT="$SCRIPT_DIR/static/tailwind.css"

PORT="${DASHBOARD_PORT:-8080}"
HOST="${DASHBOARD_HOST:-0.0.0.0}"

ensure_tailwindcss() {
    if [[ ! -x "$TAILWIND_BIN" ]]; then
        echo "tailwindcss binary not found, downloading..."
        curl -sL "https://github.com/tailwindlabs/tailwindcss/releases/latest/download/tailwindcss-linux-x64" \
            -o "$TAILWIND_BIN"
        chmod +x "$TAILWIND_BIN"
        echo "tailwindcss downloaded."
    fi
}

stop_server() {
    if [[ -f "$PID_FILE" ]]; then
        local pgid
        pgid=$(cat "$PID_FILE")
        if kill -0 "-$pgid" 2>/dev/null || kill -0 "$pgid" 2>/dev/null; then
            echo "Stopping dashboard (PGID $pgid)..."
            kill -- "-$pgid" 2>/dev/null || true
            for _ in $(seq 1 10); do
                kill -0 "-$pgid" 2>/dev/null || break
                sleep 0.5
            done
            if kill -0 "-$pgid" 2>/dev/null; then
                echo "WARN: Dashboard did not exit cleanly, sending SIGKILL"
                kill -9 -- "-$pgid" 2>/dev/null || true
            fi
            echo "Stopped."
        else
            echo "PGID $pgid not running (stale pid file)."
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
        local pgid
        pgid=$(cat "$PID_FILE")
        if kill -0 "-$pgid" 2>/dev/null || kill -0 "$pgid" 2>/dev/null; then
            echo "dashboard running: PGID $pgid, port $PORT"
            return 0
        else
            echo "dashboard NOT running (stale pid file, PGID $pgid)"
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
    existing_pgid=$(cat "$PID_FILE")
    if kill -0 "-$existing_pgid" 2>/dev/null || kill -0 "$existing_pgid" 2>/dev/null; then
        echo "Dashboard already running (PGID $existing_pgid). Stopping first..."
        stop_server
        sleep 1
    else
        rm -f "$PID_FILE"
    fi
fi

ensure_tailwindcss

# Start with --reload for hot reloading
mkdir -p "$(dirname "$LOG_FILE")"

echo "Starting dashboard: host=$HOST, port=$PORT (hot-reload enabled, tailwind --watch)"

# Launch tailwindcss --watch and uvicorn as two background processes sharing a
# process group.  setsid creates a new session so the child bash becomes the
# group leader; its PID == PGID of both children.  One kill -- -$PGID stops all.
setsid bash -c "
  cd \"$REPO_ROOT\"
  \"$TAILWIND_BIN\" \
    --cwd \"$SCRIPT_DIR\" \
    -i tailwind.input.css \
    -o static/tailwind.css \
    --watch=always \
    >> \"$LOG_FILE\" 2>&1 &
  SSL_ARGS=\"\"
  if [[ -f \"$REPO_ROOT/data/tls.crt\" && -f \"$REPO_ROOT/data/tls.key\" ]]; then
    SSL_ARGS=\"--ssl-certfile $REPO_ROOT/data/tls.crt --ssl-keyfile $REPO_ROOT/data/tls.key\"
  fi
  \"$VENV\" -m uvicorn tools.dashboard.server:app \
    --host \"$HOST\" \
    --port \"$PORT\" \
    --reload \
    --reload-dir tools/dashboard \
    --reload-exclude 'tools/dashboard/tests/*' \
    \$SSL_ARGS \
    >> \"$LOG_FILE\" 2>&1 &
  wait
" &

PGID=$!
echo "$PGID" > "$PID_FILE"

sleep 2
if kill -0 "-$PGID" 2>/dev/null || kill -0 "$PGID" 2>/dev/null; then
    echo "dashboard started: PGID $PGID"
    if [[ -f "$REPO_ROOT/data/tls.crt" ]]; then
        echo "  URL: https://$HOST:$PORT (TLS enabled)"
    else
        echo "  URL: http://$HOST:$PORT"
    fi
    echo "  Log: $LOG_FILE"
    echo "  PID file: $PID_FILE (stores PGID)"
    echo "  Hot-reload: tools/dashboard/"
    echo "  Tailwind:   watching tailwind.input.css → static/tailwind.css"
else
    echo "ERROR: dashboard failed to start. Check $LOG_FILE" >&2
    rm -f "$PID_FILE"
    exit 1
fi
