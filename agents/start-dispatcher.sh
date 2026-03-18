#!/usr/bin/env bash
# Start the dispatcher process.
#
# Usage: ./agents/start-dispatcher.sh [--stop] [--status] [--restart]
#
# Options:
#   --stop      Stop the running dispatcher
#   --status    Check if dispatcher is running
#   --restart   Stop then start
#
# Config (env vars or defaults):
#   DISPATCH_INTERVAL   Poll interval in seconds (default: 5)
#   DISPATCH_MAX_CONCURRENT  Max concurrent agents (default: 2)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
PID_FILE="$REPO_ROOT/data/dispatcher.pid"
LOG_FILE="$REPO_ROOT/data/dispatcher.log"
VENV="$REPO_ROOT/.venv/bin/python"

INTERVAL="${DISPATCH_INTERVAL:-5}"
MAX_CONCURRENT="${DISPATCH_MAX_CONCURRENT:-2}"

export PATH="$HOME/go/bin:$PATH"

stop_server() {
    if [[ -f "$PID_FILE" ]]; then
        local pid
        pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            echo "Stopping dispatcher (PID $pid)..."
            kill "$pid"
            for _ in $(seq 1 10); do
                kill -0 "$pid" 2>/dev/null || break
                sleep 0.5
            done
            if kill -0 "$pid" 2>/dev/null; then
                echo "WARN: Dispatcher did not exit cleanly, sending SIGKILL"
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
            echo "dispatcher running: PID $pid, interval ${INTERVAL}s, max-concurrent $MAX_CONCURRENT"
            return 0
        else
            echo "dispatcher NOT running (stale pid file, PID $pid)"
            rm -f "$PID_FILE"
            return 1
        fi
    else
        echo "dispatcher NOT running (no pid file)"
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
        echo "Dispatcher already running (PID $existing_pid). Stopping first..."
        stop_server
        sleep 1
    else
        rm -f "$PID_FILE"
    fi
fi

# Start
mkdir -p "$(dirname "$LOG_FILE")"

echo "Starting dispatcher: interval=${INTERVAL}s, max-concurrent=$MAX_CONCURRENT"
nohup "$VENV" -u -m agents.dispatcher \
    --loop \
    --interval "$INTERVAL" \
    --max-concurrent "$MAX_CONCURRENT" \
    >> "$LOG_FILE" 2>&1 &

DISPATCHER_PID=$!
echo "$DISPATCHER_PID" > "$PID_FILE"

sleep 1
if kill -0 "$DISPATCHER_PID" 2>/dev/null; then
    echo "dispatcher started: PID $DISPATCHER_PID"
    echo "  Log: $LOG_FILE"
    echo "  PID file: $PID_FILE"
else
    echo "ERROR: dispatcher failed to start. Check $LOG_FILE" >&2
    rm -f "$PID_FILE"
    exit 1
fi
