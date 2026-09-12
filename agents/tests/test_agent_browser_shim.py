"""Behaviour of the agent-browser session shim (agents/agent_browser_shim.sh).

These tests launch real Chrome through the real agent-browser binary in an
isolated sidecar directory (AGENT_BROWSER_SOCKET_DIR), so the guard, the
sentinel, and the orphan sweep are proven on actual processes rather than
mocks. Notifications go to a stub HTTP server standing in for the dashboard's
``/api/session/notify``. Skipped where agent-browser or Chrome is missing.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SHIM = Path(__file__).resolve().parents[1] / "agent_browser_shim.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("agent-browser") is None, reason="agent-browser binary not installed"
)


class _Notify(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        self.posts: list[dict] = []
        super().__init__(("127.0.0.1", 0), _Handler)


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.posts.append(
            {"path": self.path, "auth": self.headers.get("Authorization"), "body": body}
        )
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def log_message(self, *_):
        pass


@pytest.fixture
def notify_server():
    server = _Notify()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()


@pytest.fixture
def shim(tmp_path, notify_server):
    """A callable running the shim with an isolated state dir and stub dashboard."""
    state = tmp_path / "state"
    state.mkdir()
    base_env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"AGENT_BROWSER_SESSION", "AGENT_BROWSER_ALLOW_MANY", "XDG_RUNTIME_DIR"}
    }
    base_env.update(
        {
            "AGENT_BROWSER_SOCKET_DIR": str(state),
            "GRAPH_API": f"http://127.0.0.1:{notify_server.server_address[1]}",
            "AUTONOMY_SESSION": "shim-test",
            "CROSSTALK_TOKEN": "test-token",
            "AGENT_BROWSER_SENTINEL_POLL_S": "1",
            "AGENT_BROWSER_IDLE_TIMEOUT_MS": "60000",
        }
    )

    def run(*args: str, timeout: int = 60, **overrides: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(SHIM), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**base_env, **overrides},
        )

    run.state = state  # type: ignore[attr-defined]
    run.posts = notify_server.posts  # type: ignore[attr-defined]

    probe = run("--session", "probe", "open", "about:blank")
    if probe.returncode != 0:
        run("reap", "--all")
        pytest.skip(f"agent-browser cannot launch Chrome here: {probe.stderr[:200]}")
    run("--session", "probe", "close")
    _wait(lambda: not (state / "probe.sentinel").exists(), 10)
    try:
        yield run
    finally:
        for reg in state.glob("*.sentinel"):
            try:
                os.kill(int(reg.read_text().split()[0]), signal.SIGKILL)
            except (OSError, ValueError, IndexError):
                pass
        run("reap", "--all")


def _wait(cond, seconds: float, step: float = 0.25) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(step)
    return cond()


def _counts(shim) -> dict[str, int]:
    out = shim("ps", "--count").stdout.strip()
    return {k: int(v) for k, v in (kv.split("=") for kv in out.split())}


def _daemon_pid(shim, name: str) -> int:
    return int((shim.state / f"{name}.pid").read_text().strip())


def test_open_guard_refuses_second_session_until_told_otherwise(shim):
    assert shim("--session", "a", "open", "about:blank").returncode == 0

    refused = shim("--session", "b", "open", "about:blank")
    assert refused.returncode == 2
    assert "refusing to open browser session 'b'" in refused.stderr
    assert "\n  a " in refused.stderr  # the live one is named
    assert "--replace" in refused.stderr and "--new" in refused.stderr
    assert _counts(shim)["sessions"] == 1

    # Same name is a reuse, not a second browser: always allowed.
    assert shim("--session", "a", "open", "about:blank").returncode == 0

    assert shim("--session", "b", "open", "--new", "about:blank").returncode == 0
    assert _counts(shim)["sessions"] == 2
    listing = shim("ps").stdout
    assert listing.startswith("SESSION") and "\na " in listing and "\nb " in listing

    # --replace closes every other live session first, then opens.
    assert shim("--session", "c", "open", "--replace", "about:blank").returncode == 0
    assert _wait(lambda: _counts(shim) == {"sessions": 1, "orphans": 0, "rss_mb": _counts(shim)["rss_mb"]}, 10)
    assert "\nc " in shim("ps").stdout

    # The env opt-out used by test suites lifts the guard entirely.
    assert shim("--session", "d", "open", "about:blank", AGENT_BROWSER_ALLOW_MANY="1").returncode == 0
    assert _counts(shim)["sessions"] == 2

    # Closing on purpose never produces a notification.
    shim("close", "--all")
    time.sleep(3)
    assert shim.posts == []
    assert _counts(shim)["sessions"] == 0


def test_idle_limit_closes_browser_and_notifies_session(shim):
    assert shim("--session", "idle", "open", "about:blank", AGENT_BROWSER_IDLE_TIMEOUT_MS="3000").returncode == 0
    assert (shim.state / "idle.sentinel").exists()

    assert _wait(lambda: any(p["body"].get("status") == "idle-closed" for p in shim.posts), 25)
    post = next(p for p in shim.posts if p["body"]["status"] == "idle-closed")
    assert post["path"] == "/api/session/notify"
    assert post["auth"] == "Bearer test-token"
    body = post["body"]
    assert body["tmux_session"] == "shim-test"
    assert body["kind"] == "agent-browser"
    assert body["notification_id"].startswith("agent-browser:idle:")
    assert "closed automatically" in body["summary"] and "'idle'" in body["summary"]

    assert _counts(shim) == {"sessions": 0, "orphans": 0, "rss_mb": 0}
    assert not (shim.state / "idle.pid").exists()
    assert not (shim.state / "idle.sentinel").exists()


def test_daemon_death_is_reported_and_orphaned_chrome_is_reaped(shim):
    assert shim("--session", "crash", "open", "about:blank").returncode == 0
    os.kill(_daemon_pid(shim, "crash"), signal.SIGKILL)

    assert _wait(lambda: any(p["body"].get("status") == "exited" for p in shim.posts), 25)
    summary = next(p["body"]["summary"] for p in shim.posts if p["body"]["status"] == "exited")
    assert "exited without being closed" in summary
    assert "reaped 1 orphaned Chrome tree" in summary
    assert _counts(shim)["orphans"] == 0


def test_ps_shows_orphans_and_reap_removes_them(shim):
    # A sentinel that polls slowly stays out of the way so `reap` is what acts.
    assert shim("--session", "orph", "open", "about:blank", AGENT_BROWSER_SENTINEL_POLL_S="600").returncode == 0
    daemon = _daemon_pid(shim, "orph")
    os.kill(daemon, signal.SIGKILL)
    assert _wait(lambda: _counts(shim)["orphans"] == 1, 10)

    listing = shim("ps").stdout
    assert "Orphaned Chrome trees" in listing and "/tmp/agent-browser-chrome-" in listing
    profile = next(
        tok for line in listing.splitlines() for tok in line.split() if tok.startswith("/tmp/agent-browser-chrome-")
    )
    assert Path(profile).is_dir()

    reaped = shim("reap")
    assert reaped.returncode == 0
    assert "reaped 1 orphaned Chrome tree" in reaped.stdout
    assert _counts(shim)["orphans"] == 0
    assert not Path(profile).exists()
    assert not any(profile in line for line in subprocess.run(["ps", "-eww", "-o", "args="], capture_output=True, text=True).stdout.splitlines())


def test_reap_idle_closes_only_sessions_past_the_limit(shim):
    assert shim("--session", "old", "open", "about:blank").returncode == 0
    old_stamp = shim.state / "old.last"
    os.utime(old_stamp, (time.time() - 600, time.time() - 600))
    assert shim("--session", "fresh", "open", "--new", "about:blank").returncode == 0

    out = shim("reap", "--idle", "5").stdout
    assert "closed session 'old'" in out and "fresh" not in out.split("Live now")[0]
    assert _wait(lambda: _counts(shim)["sessions"] == 1, 10)
    assert "\nfresh " in shim("ps").stdout

    assert shim("reap", "--bogus").returncode == 2


def test_verbs_without_a_browser(shim):
    listing = shim("ps")
    assert listing.returncode == 0 and "(no live browser sessions)" in listing.stdout
    assert _counts(shim) == {"sessions": 0, "orphans": 0, "rss_mb": 0}
    assert shim("reap").stdout.startswith("reaped 0 orphaned")
    # Everything else is passed to the real binary untouched.
    assert "agent-browser" in shim("--version").stdout
