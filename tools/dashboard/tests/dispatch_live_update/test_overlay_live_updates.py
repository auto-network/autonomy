"""auto-yaw58 test 4 — browser-level live updates, fully unmocked.

Opens a real session-viewer page in agent-browser against a live dashboard
harness with a planted dispatch session. Subscribes to ``session:messages``
via EventSource, writes a JSONL entry, and asserts both the viewer state and
the browser-observed broadcast advance.

The dispatch-tail/session-id seam is covered separately in the unit/integration
tests in this directory. This browser test keeps its focus narrower: given a
real registered live session, does the session viewer grow when the monitor
broadcasts a new entry?
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from .conftest import insert_raw_dispatch_row, append_jsonl


def _has_agent_browser() -> bool:
    try:
        r = subprocess.run(
            ["agent-browser", "--help"], capture_output=True, timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


pytestmark = [
    pytest.mark.skipif(
        not _has_agent_browser(), reason="agent-browser not available",
    ),
]


from tools.dashboard.tests._xdist import bind_free_port, worker_test_port

TEST_PORT = worker_test_port(8090)


def _ab_eval(js: str):
    """Eval JS via stdin, unwrap response."""
    wrapped = f"(() => {{\n{js}\n}})()"
    r = subprocess.run(
        ["agent-browser", "--json", "eval", "--stdin"],
        input=wrapped, capture_output=True, text=True, timeout=15,
    )
    for line in reversed(r.stdout.strip().split("\n")):
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
            if isinstance(parsed, dict) and "success" in parsed and "data" in parsed:
                d = parsed["data"]
                if isinstance(d, dict) and "result" in d:
                    return d["result"]
                return d
        except Exception:
            continue
    return None


def _ab_raw(*args, timeout: int = 10):
    return subprocess.run(
        ["agent-browser", *args],
        capture_output=True, text=True, timeout=timeout,
    ).stdout


@pytest.fixture
def live_dashboard(tmp_path, monkeypatch):
    """Start a real dashboard server subprocess against isolated DBs."""
    dashboard_db = tmp_path / "dashboard.db"
    dispatch_db = tmp_path / "dispatch.db"
    agent_runs = tmp_path / "agent-runs"
    agent_runs.mkdir(parents=True)

    from .conftest import init_dashboard_db, init_dispatch_db
    init_dashboard_db(dashboard_db)
    init_dispatch_db(dispatch_db)

    env = os.environ.copy()
    env["DASHBOARD_DB"] = str(dashboard_db)
    env["DISPATCH_DB"] = str(dispatch_db)
    env["DASHBOARD_AGENT_RUNS_DIR"] = str(agent_runs)
    env.pop("DASHBOARD_MOCK", None)
    # This server runs UNMOCKED, so startup restores the EventBus snapshot
    # — with the per-worker default path it replays another test file's
    # cached session:messages into this file's subscribers (the overlay
    # then sees a foreign session_id and drops every event). Isolate it.
    env["DASHBOARD_EVENT_BUS_STATE"] = str(tmp_path / "event_bus.state")
    repo_root = str(Path(__file__).resolve().parents[4])
    env["PYTHONPATH"] = repo_root

    # Bind a kernel-assigned free port and hand its descriptor to uvicorn via
    # --fd. worker_test_port is worker-index derived with no session dimension,
    # so a fixed port can be answered by another session's server (port-collision
    # report, auto-0812-211339). This server runs UNMOCKED, so the mock nonce
    # endpoint is unavailable — the OS-assigned port alone makes collisions
    # impossible, which is what the probe below then relies on.
    global TEST_PORT
    sock, TEST_PORT = bind_free_port()
    proc = subprocess.Popen(
        ["python3", "-m", "uvicorn", "tools.dashboard.server:app",
         "--fd", str(sock.fileno())],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, cwd=repo_root, pass_fds=(sock.fileno(),),
    )
    sock.close()
    import httpx
    for _ in range(40):
        try:
            r = httpx.get(f"http://localhost:{TEST_PORT}/dispatch", timeout=1)
            if r.status_code in (200, 404):
                break
        except Exception:
            pass
        time.sleep(0.5)
    else:
        proc.send_signal(signal.SIGTERM)
        raise RuntimeError("server failed to start")

    yield {
        "tmp_path": tmp_path,
        "dashboard_db": dashboard_db,
        "agent_runs": agent_runs,
        "port": TEST_PORT,
    }

    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


class TestOverlayLiveUpdatesUnmocked:
    """#4 — session viewer receives live session:messages from a real broadcast."""

    def test_overlay_receives_live_updates_unmocked(self, live_dashboard):
        port = live_dashboard["port"]
        dashboard_db = live_dashboard["dashboard_db"]
        agent_runs = live_dashboard["agent_runs"]

        tmux_name = "auto-yaw58live-0420-100004"
        session_uuid = "44444444-5555-6666-7777-888888888888"
        assert tmux_name != session_uuid

        sess_dir = agent_runs / tmux_name / "sessions" / "autonomy"
        sess_dir.mkdir(parents=True)
        jsonl = sess_dir / f"{session_uuid}.jsonl"
        append_jsonl(jsonl, "seed")

        insert_raw_dispatch_row(
            dashboard_db,
            tmux_name=tmux_name,
            session_uuid=session_uuid,
            jsonl_path=str(jsonl),
            bead_id="auto-yaw58live",
        )

        # Register via the real HTTP endpoint
        import httpx
        r = httpx.post(
            f"http://localhost:{port}/api/monitor/register",
            json={
                "tmux_name": tmux_name,
                "type": "dispatch",
                "jsonl_path": str(jsonl),
                "bead_id": "auto-yaw58live",
                "project": "autonomy",
            },
            timeout=5,
        )
        assert r.status_code == 200, r.text

        # Open the session viewer page directly against the registered tmux
        # session. The lower-level seam tests already cover the dispatch-tail
        # contract; this browser test focuses on end-to-end live updates.
        _ab_raw("close")
        _ab_raw("open", f"http://localhost:{port}/session/autonomy/{tmux_name}",
                "--ignore-https-errors", timeout=15)
        # Poll for the viewer's Alpine store row instead of a fixed sleep.
        _deadline = time.time() + 25
        while time.time() < _deadline:
            _ready = _ab_eval(
                "return !!(window.Alpine && Alpine.store('sessions')"
                f" && Alpine.store('sessions')['{tmux_name}']);"
            )
            if _ready is True:
                break
            time.sleep(0.5)

        opened = _ab_eval(f"""
            var viewer = null;
            var viewers = document.querySelectorAll('[x-data]');
            for (var i = 0; i < viewers.length; i++) {{
                var cmp = typeof Alpine !== 'undefined' ? Alpine.$data(viewers[i]) : null;
                if (cmp && cmp._mode === 'page') {{ viewer = cmp; break; }}
            }}
            return viewer ? 'opened' : 'missing';
        """)
        assert opened == "opened", f"session viewer unavailable: {opened!r}"
        time.sleep(3)

        # Capture the viewer's sessionKey AND subscribe to broadcasts
        setup = _ab_eval(f"""
            var viewer = null;
            var viewers = document.querySelectorAll('[x-data]');
            for (var i = 0; i < viewers.length; i++) {{
                var cmp = typeof Alpine !== 'undefined' ? Alpine.$data(viewers[i]) : null;
                if (cmp && cmp._mode === 'page') {{ viewer = cmp; break; }}
            }}
            if (!viewer) return {{error: 'no viewer'}};

            window._yawBroadcasts = [];
            var es = new EventSource('/api/events?topics=session:messages');
            es.addEventListener('session:messages', function(e) {{
                try {{ window._yawBroadcasts.push(JSON.parse(e.data)); }} catch (err) {{}}
            }});
            window._yawEs = es;

            return {{
                sessionKey: viewer.sessionKey || viewer.sessionId || null,
                entries_initial: Array.isArray(viewer.entries) ? viewer.entries.length : -1,
            }};
        """)
        assert isinstance(setup, dict) and not setup.get("error"), (
            f"Viewer setup failed: {setup!r}"
        )
        session_key = setup.get("sessionKey")
        entries_initial = setup.get("entries_initial")

        assert session_key in (tmux_name, session_uuid), (
            f"Viewer sessionKey={session_key!r} is neither the planted "
            f"tmux_name ({tmux_name!r}) nor the session_uuid ({session_uuid!r})."
        )

        # Append a real JSONL entry
        append_jsonl(jsonl, "overlay-live-test payload")
        time.sleep(4)  # inotify + tailer + SSE + client

        # Read the overlay's current entries length + the first broadcast's session_id
        result = _ab_eval("""
            var viewer = null;
            var viewers = document.querySelectorAll('[x-data]');
            for (var i = 0; i < viewers.length; i++) {
                var cmp = typeof Alpine !== 'undefined' ? Alpine.$data(viewers[i]) : null;
                if (cmp && cmp._mode === 'page') { viewer = cmp; break; }
            }
            var broadcasts = window._yawBroadcasts || [];
            try { window._yawEs && window._yawEs.close(); } catch (e) {}
            return {
                entries_final: viewer && Array.isArray(viewer.entries)
                    ? viewer.entries.length : -1,
                broadcast_count: broadcasts.length,
                first_broadcast_sid: broadcasts[0] ? broadcasts[0].session_id : null,
            };
        """)

        assert isinstance(result, dict), f"read-back failed: {result!r}"

        # First check: a broadcast must have arrived
        assert result.get("broadcast_count", 0) >= 1, (
            f"No session:messages broadcast arrived during the test. "
            f"broadcast_count={result.get('broadcast_count')}. Monitor "
            "isn't firing for this dispatch, or SSE transport dropped the "
            "event. Cannot evaluate the seam without a broadcast."
        )

        # The seam at user-observation level
        broadcast_sid = result.get("first_broadcast_sid")
        assert broadcast_sid == session_key, (
            f"Seam at user layer: overlay subscribed on sessionKey="
            f"{session_key!r} but monitor broadcast arrived with "
            f"session_id={broadcast_sid!r}. The overlay's SSE handler "
            "filters by session_id; this mismatch means every incoming "
            "event is dropped. User-visible symptom: overlay never "
            "updates live."
        )

        # User-visible symptom: entries must have grown
        assert result.get("entries_final", 0) > entries_initial, (
            f"Overlay entries.length did not grow: initial={entries_initial}, "
            f"final={result.get('entries_final')}. Broadcast arrived "
            f"(session_id={broadcast_sid!r}) and matched sessionKey="
            f"{session_key!r}, but the overlay's append path didn't fire. "
            "Downstream of the seam there's a second bug."
        )
