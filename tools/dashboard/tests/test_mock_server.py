"""
Tests for the self-daemonizing mock server CLI (tools.dashboard.mock_server).

Tests the CLI commands: start, stop, status — and the fork/daemon lifecycle.
"""
import json
import os
import signal
import time
import urllib.request
import urllib.error

import pytest

from tools.dashboard.mock_server import main, _pid_file, _read_pid, RUN_DIR


# Use high ports to avoid collisions with other tests
TEST_PORT = 8091


def _server_responds(port: int) -> bool:
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/beads/list", method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


def _stop_server(port: int):
    """Force-stop any server on port, ignore errors."""
    try:
        main(["stop", "--port", str(port)])
    except SystemExit:
        pass
    # Also kill via PID file if still alive
    pid = _read_pid(port)
    if pid:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    _pid_file(port).unlink(missing_ok=True)


@pytest.fixture(autouse=True)
def cleanup_server():
    """Ensure test server is stopped after each test."""
    yield
    _stop_server(TEST_PORT)
    _stop_server(TEST_PORT + 1)


class TestStartCommand:

    def test_start_creates_server(self):
        rc = main(["start", "--port", str(TEST_PORT)])
        assert rc == 0
        assert _server_responds(TEST_PORT)
        assert _read_pid(TEST_PORT) is not None

    def test_start_with_custom_fixture(self, tmp_path):
        fixture = {
            "beads": [
                {"id": "auto-custom", "title": "Custom fixture bead", "priority": 0, "status": "open"},
            ],
            "runs": [],
        }
        fixture_path = tmp_path / "fixtures.json"
        fixture_path.write_text(json.dumps(fixture))

        rc = main(["start", "--port", str(TEST_PORT), "--fixture", str(fixture_path)])
        assert rc == 0

        # Verify custom data is served
        with urllib.request.urlopen(f"http://127.0.0.1:{TEST_PORT}/api/beads/list", timeout=3) as resp:
            data = json.loads(resp.read())
        assert len(data) == 1
        assert data[0]["title"] == "Custom fixture bead"

    def test_start_rejects_duplicate(self):
        rc1 = main(["start", "--port", str(TEST_PORT)])
        assert rc1 == 0

        rc2 = main(["start", "--port", str(TEST_PORT)])
        assert rc2 == 1  # already running

    def test_start_rejects_missing_fixture(self):
        rc = main(["start", "--port", str(TEST_PORT), "--fixture", "/nonexistent/file.json"])
        assert rc == 1


class TestStopCommand:

    def test_stop_kills_server(self):
        main(["start", "--port", str(TEST_PORT)])
        assert _server_responds(TEST_PORT)

        rc = main(["stop", "--port", str(TEST_PORT)])
        assert rc == 0

        time.sleep(0.5)
        assert not _server_responds(TEST_PORT)
        assert _read_pid(TEST_PORT) is None

    def test_stop_when_not_running(self):
        rc = main(["stop", "--port", str(TEST_PORT)])
        assert rc == 0  # graceful no-op


class TestStatusCommand:

    def test_status_running(self):
        main(["start", "--port", str(TEST_PORT)])
        rc = main(["status", "--port", str(TEST_PORT)])
        assert rc == 0

    def test_status_not_running(self):
        rc = main(["status", "--port", str(TEST_PORT)])
        assert rc == 1


class TestDaemonSurvival:
    """Verify the child process survives after parent exits — the core SIGPIPE fix."""

    def test_server_survives_parent_exit(self):
        """Start server, verify it keeps running even after main() returns."""
        rc = main(["start", "--port", str(TEST_PORT)])
        assert rc == 0

        pid = _read_pid(TEST_PORT)
        assert pid is not None

        # Wait a moment — if the child had inherited a doomed pipe, it would
        # SIGPIPE after the parent's stdout closed
        time.sleep(1)
        assert _server_responds(TEST_PORT), "Server died after parent exited (SIGPIPE?)"

    def test_different_ports_coexist(self):
        """Two servers on different ports can run simultaneously."""
        rc1 = main(["start", "--port", str(TEST_PORT)])
        rc2 = main(["start", "--port", str(TEST_PORT + 1)])
        assert rc1 == 0
        assert rc2 == 0
        assert _server_responds(TEST_PORT)
        assert _server_responds(TEST_PORT + 1)
