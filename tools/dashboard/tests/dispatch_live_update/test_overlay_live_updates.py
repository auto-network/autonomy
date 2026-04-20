"""auto-yaw58 test 4 — browser-level, fully unmocked.

Opens /dispatch in agent-browser against a real dashboard harness with a
planted dispatch row. Reads the overlay's sessionKey. Subscribes to
session:messages via EventSource. Writes a JSONL entry. Asserts the
overlay's entries grow AND that the first observed broadcast's
session_id matches the overlay's sessionKey.

This mirrors how a user observes the bug: the close+reopen refreshes
entries (batch path), but live-streaming doesn't work. Test asserts
both work; fails on master because sessionKey = session_uuid and
broadcast session_id = tmux_name.

FAIL-REASON on master: either (a) overlay sessionKey != broadcast
session_id, proving the seam, OR (b) entries.length doesn't grow after
the JSONL write, proving the user-visible symptom.
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


from tools.dashboard.tests._xdist import worker_test_port

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
    repo_root = str(Path(__file__).resolve().parents[4])
    env["PYTHONPATH"] = repo_root

    subprocess.run(
        ["pkill", "-f", f"uvicorn.*{TEST_PORT}"],
        capture_output=True, timeout=3,
    )
    time.sleep(1)

    proc = subprocess.Popen(
        ["python3", "-m", "uvicorn", "tools.dashboard.server:app",
         "--host", "127.0.0.1", "--port", str(TEST_PORT)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, cwd=repo_root,
    )
    import httpx
    for _ in range(20):
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
    """#4 — overlay receives live session:messages from a real broadcast."""

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

        # Open /dispatch and trigger the overlay
        _ab_raw("close")
        _ab_raw("open", f"http://localhost:{port}/dispatch",
                "--ignore-https-errors", timeout=15)
        time.sleep(3)

        opened = _ab_eval(f"""
            if (typeof window._livePanelLoad === 'function') {{
                window._livePanelLoad({json.dumps(tmux_name)}, true);
                return 'opened';
            }}
            return 'missing';
        """)
        assert opened == "opened", f"_livePanelLoad unavailable: {opened!r}"
        time.sleep(3)

        # Capture overlay's sessionKey AND subscribe to broadcasts
        setup = _ab_eval(f"""
            var overlay = null;
            var viewers = document.querySelectorAll('[x-data]');
            for (var i = 0; i < viewers.length; i++) {{
                var cmp = typeof Alpine !== 'undefined' ? Alpine.$data(viewers[i]) : null;
                if (cmp && cmp._mode === 'overlay') {{ overlay = cmp; break; }}
            }}
            if (!overlay) return {{error: 'no overlay'}};

            window._yawBroadcasts = [];
            var es = new EventSource('/api/events?topics=session:messages');
            es.addEventListener('session:messages', function(e) {{
                try {{ window._yawBroadcasts.push(JSON.parse(e.data)); }} catch (err) {{}}
            }});
            window._yawEs = es;

            return {{
                sessionKey: overlay.sessionKey || overlay.sessionId || null,
                entries_initial: Array.isArray(overlay.entries) ? overlay.entries.length : -1,
            }};
        """)
        assert isinstance(setup, dict) and not setup.get("error"), (
            f"Overlay setup failed: {setup!r}"
        )
        session_key = setup.get("sessionKey")
        entries_initial = setup.get("entries_initial")

        assert session_key in (tmux_name, session_uuid), (
            f"Overlay's sessionKey={session_key!r} is neither the planted "
            f"tmux_name ({tmux_name!r}) nor the session_uuid ({session_uuid!r})."
        )

        # Append a real JSONL entry
        append_jsonl(jsonl, "overlay-live-test payload")
        time.sleep(4)  # inotify + tailer + SSE + client

        # Read the overlay's current entries length + the first broadcast's session_id
        result = _ab_eval("""
            var overlay = null;
            var viewers = document.querySelectorAll('[x-data]');
            for (var i = 0; i < viewers.length; i++) {
                var cmp = typeof Alpine !== 'undefined' ? Alpine.$data(viewers[i]) : null;
                if (cmp && cmp._mode === 'overlay') { overlay = cmp; break; }
            }
            var broadcasts = window._yawBroadcasts || [];
            try { window._yawEs && window._yawEs.close(); } catch (e) {}
            return {
                entries_final: overlay && Array.isArray(overlay.entries)
                    ? overlay.entries.length : -1,
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
