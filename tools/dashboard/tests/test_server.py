"""
Test server wrapper — boots dashboard with tmux mocked.

Usage:
    DASHBOARD_DB=/path/to/test.db python3 -m tools.dashboard.tests.test_server

Patches SessionMonitor._check_tmux before importing the app,
so mock sessions stay alive through the monitor's liveness checks.
"""
import os
import sys

# Patch tmux before anything imports session_monitor
MOCK_SESSIONS = set(os.environ.get("MOCK_SESSIONS", "").split(","))


def _patched_check_tmux(name: str) -> bool:
    return name in MOCK_SESSIONS


# Import and patch
from tools.dashboard import session_monitor
session_monitor.SessionMonitor._check_tmux = staticmethod(_patched_check_tmux)

# Now import the app (which imports session_monitor)
from tools.dashboard.server import app

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("TEST_PORT", "8082"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
