"""
Test: dispatch Last column must not show NaN.

The dispatch_db stores last_activity as ISO DATETIME strings.
server.py must convert them to Unix seconds before sending to JS.
The JS formatter does `Date.now()/1000 - ts` — if ts is a string, NaN.

Run: pytest tools/dashboard/tests/dispatch/test_last_activity.py -v
"""
import asyncio
import json
import os
import signal
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest
import httpx

from tools.dashboard.tests import fixtures


# ── Fixtures ─────────────────────────────────────────────────────────

def _make_dispatch_fixture(last_activity=None):
    """Build a mock fixture with a RUNNING dispatch run."""
    run = {
        "id": "run-last-act",
        "bead_id": "auto-dull",
        "status": "RUNNING",
        "started_at": "2026-03-27T15:00:00Z",
        "last_activity": last_activity,
        "container_name": "agent-auto-dull",
        "image": "agent:latest",
        "token_count": 5000,
        "tool_count": 12,
        "turn_count": 8,
        "last_snippet": "Working on the fix...",
    }
    return {
        "active_sessions": fixtures.STANDARD_SESSIONS,
        "beads": [
            {"id": "auto-dull", "title": "Fix NaN", "priority": 1,
             "status": "in_progress", "labels": ["readiness:approved"]},
        ],
        "runs": [run],
        "experiments": [fixtures.make_experiment(fixtures.TEST_EXPERIMENT_ID)],
    }


# ── Unit test: _collect_dispatch_data conversion ─────────────────────

class TestLastActivityConversion:
    """Verify server converts ISO last_activity to Unix seconds."""

    @pytest.fixture(autouse=True)
    def _setup_mock(self, tmp_path):
        fixture_path = tmp_path / "fixtures.json"
        self.fixture_path = fixture_path
        os.environ["DASHBOARD_MOCK"] = str(fixture_path)
        import importlib
        from tools.dashboard.dao import mock as mock_mod
        importlib.reload(mock_mod)
        from tools.dashboard import server
        importlib.reload(server)
        self.server = server
        yield
        os.environ.pop("DASHBOARD_MOCK", None)

    def _write_fixture(self, last_activity):
        data = _make_dispatch_fixture(last_activity)
        fixtures.write_fixture(data, self.fixture_path)

    def _collect(self):
        return asyncio.run(self.server._collect_dispatch_data())

    def test_iso_string_converted_to_unix(self):
        """ISO datetime string must become a numeric Unix timestamp."""
        self._write_fixture("2026-03-27T16:00:00")
        result = self._collect()
        active = result["active"]
        assert len(active) >= 1
        last_act = active[0]["last_activity"]
        assert isinstance(last_act, (int, float)), \
            f"last_activity should be numeric, got {type(last_act).__name__}: {last_act}"
        # 2026-03-27T16:00:00 UTC → should be around 1774742400
        assert 1774000000 < last_act < 1776000000, f"Unexpected value: {last_act}"

    def test_null_last_activity_stays_none(self):
        """Null last_activity must remain None (not crash)."""
        self._write_fixture(None)
        result = self._collect()
        active = result["active"]
        assert len(active) >= 1
        assert active[0]["last_activity"] is None

    def test_iso_with_timezone_converted(self):
        """ISO string with Z suffix also converts correctly."""
        self._write_fixture("2026-03-27T16:00:00Z")
        result = self._collect()
        active = result["active"]
        assert len(active) >= 1
        last_act = active[0]["last_activity"]
        assert isinstance(last_act, (int, float))


# ── Browser test: no NaN on dispatch page ────────────────────────────

from tools.dashboard.tests._xdist import worker_test_port

TEST_PORT = worker_test_port(8083)


def ab(*args, timeout=10):
    result = subprocess.run(
        ["agent-browser", "--json"] + list(args),
        capture_output=True, text=True, timeout=timeout,
    )
    for line in reversed(result.stdout.strip().split("\n")):
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
            if isinstance(parsed, dict) and "success" in parsed and "data" in parsed:
                return parsed["data"] if not parsed.get("error") else None
            return parsed
        except (json.JSONDecodeError, ValueError):
            pass
    return None


def ab_eval(js):
    wrapped = f"(() => {{\n{js}\n}})()"
    result = subprocess.run(
        ["agent-browser", "--json", "eval", "--stdin"],
        capture_output=True, text=True, timeout=10,
        input=wrapped,
    )
    for line in reversed(result.stdout.strip().split("\n")):
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
            if isinstance(parsed, dict) and "result" in parsed:
                return parsed["result"]
            if isinstance(parsed, dict) and "success" in parsed and "data" in parsed:
                data = parsed["data"]
                if isinstance(data, dict) and "result" in data:
                    return data["result"]
                return data
            return parsed
        except (json.JSONDecodeError, ValueError):
            pass
    return None


def ab_raw(*args, timeout=10):
    return subprocess.run(
        ["agent-browser"] + list(args),
        capture_output=True, text=True, timeout=timeout,
    ).stdout


@pytest.fixture(scope="module")
def dispatch_server(tmp_path_factory):
    """Boot a mock dashboard with a RUNNING dispatch that has ISO last_activity."""
    tmp = tmp_path_factory.mktemp("dispatch_last")
    fixture_path = tmp / "fixtures.json"

    data = _make_dispatch_fixture("2026-03-27T16:00:00")
    fixtures.write_fixture(data, fixture_path)

    subprocess.run(
        ["pkill", "-f", f"uvicorn.*{TEST_PORT}"],
        capture_output=True, timeout=3,
    )
    time.sleep(1)

    env = os.environ.copy()
    env["DASHBOARD_MOCK"] = str(fixture_path)
    repo_root = str(Path(__file__).resolve().parents[4])
    env["PYTHONPATH"] = repo_root

    proc = subprocess.Popen(
        ["python3", "-m", "uvicorn", "tools.dashboard.server:app",
         "--host", "127.0.0.1", "--port", str(TEST_PORT)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, cwd=repo_root,
    )

    for _ in range(20):
        try:
            if httpx.get(f"http://localhost:{TEST_PORT}/sessions", timeout=1).status_code == 200:
                break
        except Exception:
            pass
        time.sleep(0.5)
    else:
        proc.kill()
        pytest.skip("Mock server failed to start")

    yield {"proc": proc, "fixture_path": fixture_path}

    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    ab_raw("close")


class TestDispatchPageNoNaN:
    """Browser test: dispatch page must never show NaN."""

    def test_no_nan_on_dispatch_page(self, dispatch_server):
        """Active dispatch card Last column must not contain NaN."""
        ab_raw("close")
        ab_raw("open", f"http://localhost:{TEST_PORT}/dispatch",
               "--ignore-https-errors")
        time.sleep(4)  # wait for SSE data to arrive

        # Check the full page text for NaN
        text = ab_eval("return document.body.innerText;")
        assert text is not None, "Could not read page text"
        assert "NaN" not in text, \
            f"Found 'NaN' on dispatch page — last_activity not converted: {text[:500]}"
