"""
Browser smoke test for the screenshot capture pipeline.

Verifies that clicking the capture button does not throw a TypeError
(regression test for the removed setScreenshotStatus method).

Uses DASHBOARD_MOCK fixtures, test server on port 8082.
Run: pytest tools/dashboard/tests/experiment/test_browser.py -v
"""
import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from tools.dashboard.tests import fixtures
from tools.dashboard.tests._xdist import worker_test_port


TEST_PORT = worker_test_port(8082)


def ab(*args, timeout=10):
    """Run agent-browser --json, return parsed data."""
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
    """Evaluate JS via stdin IIFE, unwrap {origin, result}."""
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
def experiment_server(tmp_path_factory):
    """Boot a mock dashboard server with an experiment fixture."""
    tmp = tmp_path_factory.mktemp("experiment")
    fixture_path = tmp / "fixtures.json"
    events_path = tmp / "events.jsonl"
    events_path.write_text("")

    exp_id = fixtures.TEST_EXPERIMENT_ID
    fixture_data = {
        "active_sessions": fixtures.STANDARD_SESSIONS,
        "beads": [],
        "experiments": [fixtures.make_experiment(exp_id, title="Screenshot Test")],
    }
    fixtures.write_fixture(fixture_data, fixture_path)

    # Kill any stale server
    subprocess.run(
        ["pkill", "-f", f"uvicorn.*{TEST_PORT}"],
        capture_output=True, timeout=3,
    )
    time.sleep(1)

    env = os.environ.copy()
    env["DASHBOARD_MOCK"] = str(fixture_path)
    env["DASHBOARD_MOCK_EVENTS"] = str(events_path)
    repo_root = str(Path(__file__).resolve().parents[4])
    env["PYTHONPATH"] = repo_root

    proc = subprocess.Popen(
        ["python3", "-m", "uvicorn", "tools.dashboard.server:app",
         "--host", "127.0.0.1", "--port", str(TEST_PORT)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, cwd=repo_root,
    )

    import httpx
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

    yield {"proc": proc, "exp_id": exp_id}

    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    ab_raw("close")


class TestCaptureButton:
    """Screenshot capture button must not throw TypeError."""

    def test_capture_button_does_not_throw(self, experiment_server):
        """Capture button click must not throw TypeError (regression for setScreenshotStatus)."""
        exp_id = experiment_server["exp_id"]

        ab_raw("close")
        ab_raw("open", f"http://localhost:{TEST_PORT}/design/{exp_id}",
               "--ignore-https-errors")
        time.sleep(3)

        # Install console error collector
        ab_eval("""
            window.__consoleErrors = [];
            var origError = console.error;
            console.error = function() {
                window.__consoleErrors.push(Array.from(arguments).join(' '));
                origError.apply(console, arguments);
            };
            // Also catch uncaught errors
            window.addEventListener('error', function(e) {
                window.__consoleErrors.push(e.message || String(e));
            });
            return 'installed';
        """)

        errors_before = ab_eval("return window.__consoleErrors.length;") or 0

        # Try to click capture button — manualCaptureScreenshot will fall through
        # to html2canvas fallback (which will fail in headless), but should not
        # throw TypeError on setScreenshotStatus
        ab_eval("""
            if (typeof manualCaptureScreenshot === 'function') {
                try {
                    manualCaptureScreenshot('""" + exp_id + """', '');
                } catch(e) {
                    window.__consoleErrors.push('THROW: ' + e.constructor.name + ': ' + e.message);
                }
            }
            return 'triggered';
        """)
        time.sleep(2)

        all_errors = ab_eval("return JSON.stringify(window.__consoleErrors);") or "[]"
        try:
            error_list = json.loads(all_errors) if isinstance(all_errors, str) else all_errors or []
        except (json.JSONDecodeError, TypeError):
            error_list = []

        # Filter for TypeErrors (the specific regression we're guarding against)
        type_errors = [e for e in error_list if isinstance(e, str) and 'TypeError' in e]
        assert len(type_errors) == 0, f"TypeError on capture: {type_errors}"

    def test_screenshot_module_loaded(self, experiment_server):
        """Screenshot module must be available on the page."""
        exp_id = experiment_server["exp_id"]

        ab_raw("close")
        ab_raw("open", f"http://localhost:{TEST_PORT}/design/{exp_id}",
               "--ignore-https-errors")
        time.sleep(3)

        result = ab_eval("""
            return {
                hasModule: typeof Screenshot !== 'undefined',
                hasUrl: typeof Screenshot !== 'undefined' && typeof Screenshot._screenshotUrl === 'function',
                hasStatus: typeof Screenshot !== 'undefined' && typeof Screenshot._updateScreenshotStatus === 'function',
                hasResponse: typeof Screenshot !== 'undefined' && typeof Screenshot._handleScreenshotResponse === 'function',
            };
        """)

        assert result is not None, "Could not evaluate JS on design page"
        assert result.get("hasModule"), "Screenshot module not loaded"
        assert result.get("hasUrl"), "_screenshotUrl not available"
        assert result.get("hasStatus"), "_updateScreenshotStatus not available"
        assert result.get("hasResponse"), "_handleScreenshotResponse not available"
