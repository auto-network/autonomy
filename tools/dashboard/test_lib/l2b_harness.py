"""L2.B browser-test harness — shared between the main behavioral sweep
and per-plugin behavioral suites.

The L2.B layer boots a single ``DASHBOARD_MOCK`` uvicorn server, attaches
a single ``agent-browser`` session, and runs SPA-navigation + batched-JS
checks through both. This module exposes the building blocks so that
plugin tests collocated under ``tools/dashboard/plugins/<plugin>/tests/``
can stand up their own sweep without importing from the main test file
(which is not on their collection path).

Public surface:

* ``start_mock_server(fixture_data, tmp_path, *, port)`` — boots uvicorn,
  blocks until ``/api/dao/active_sessions`` answers; returns a state
  dict (``port`` / ``url`` / ``fixture_path`` / ``events_path`` / ``proc``).
* ``stop_mock_server(state)`` — terminates the uvicorn process, falling
  back to ``kill`` after a 5s grace period.
* ``open_browser(url)`` / ``close_browser()`` — open and close the
  module-shared agent-browser session.
* ``_navigate_and_check(path, js_checks, wait_ms)`` — SPA-navigate, run
  ONE batched JS eval, return parsed dict.
* ``_ab_eval_batch(js)`` — single ``agent-browser --json eval`` call,
  unwraps ``{data: {result}}`` for callers.
* ``_run_async_eval(js_expr)`` — variant for async IIFEs that return a
  ``JSON.stringify(...)`` string.
* ``_http_get(url)`` — tiny ``urllib`` wrapper that returns
  ``(status, body)`` and downgrades ``HTTPError`` to ``(code, "")``.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any


# ── Server lifecycle ────────────────────────────────────────────────────


def start_mock_server(
    fixture_data: dict,
    tmp_path: Path,
    *,
    port: int,
    extra_env: dict | None = None,
) -> dict:
    """Boot a ``DASHBOARD_MOCK`` uvicorn server backed by *fixture_data*.

    Writes ``fixture_data`` to ``tmp_path/fixtures.json`` and creates an
    empty ``tmp_path/events.jsonl`` for SSE replay. Blocks up to 8s
    waiting for ``/api/dao/active_sessions`` to respond. On failure the
    subprocess is killed and a ``RuntimeError`` is raised with the
    captured stdout/stderr.

    *extra_env* lets callers override or extend the subprocess
    environment — e.g. plugin tests pointing ``AUTONOMY_ORGS_DIR`` at a
    pre-seeded tmp tree so ``/api/orgs`` returns the slugs the test
    expects without depending on the host's real ``data/orgs/``.
    """
    # Per-run nonce baked into the fixture. The readiness probe asserts the
    # server it reached echoes THIS nonce before we yield — so a probe that
    # succeeds against some OTHER session's server (the port-collision failure
    # mode: worker-index-derived ports are identical across sessions on host
    # networking) is an immediate, legible error instead of a whole run of
    # confident wrong answers. See port-collision-report (crypto pillar,
    # auto-0812-211339). `port` is now ignored — see the --fd allocation below.
    nonce = uuid.uuid4().hex
    fixture_data = {**fixture_data, "__harness_nonce__": nonce}

    fixture_path = tmp_path / "fixtures.json"
    fixture_path.write_text(json.dumps(fixture_data, indent=2))

    events_path = tmp_path / "events.jsonl"
    events_path.write_text("")

    # Bind the listening socket HERE and hand its descriptor to uvicorn via
    # --fd. The kernel assigns a free port (bind to :0) that no other process —
    # in this session or any other — can also hold, so collisions are
    # impossible by construction. Unlike deriving a port and hoping, or --port 0
    # and reading it back, there is no window between choosing and binding: we
    # already own the socket.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    sock.set_inheritable(True)
    real_port = sock.getsockname()[1]

    env = {
        **os.environ,
        "DASHBOARD_MOCK": str(fixture_path),
        "DASHBOARD_MOCK_EVENTS": str(events_path),
        # repo root: tools/dashboard/test_lib/l2b_harness.py → ../../../..
        "PYTHONPATH": str(Path(__file__).resolve().parents[3]),
        # Isolate EventBus snapshot so prior runs / sibling tests do not
        # replay stale session:registry into our subscribers.
        "DASHBOARD_EVENT_BUS_STATE": str(tmp_path / "event_bus.state"),
    }
    if extra_env:
        env.update(extra_env)

    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn",
            "tools.dashboard.server:app",
            "--fd", str(sock.fileno()),
            "--log-level", "warning",
        ],
        env=env,
        pass_fds=(sock.fileno(),),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    sock.close()  # uvicorn inherited its own copy of the descriptor

    url = f"http://127.0.0.1:{real_port}"

    # Readiness + identity probe. A 200 alone proves only that SOMETHING is
    # listening; require the served nonce to match ours before trusting it.
    deadline = time.time() + 45
    ready = False
    served_nonce = None
    while time.time() < deadline:
        try:
            import urllib.request
            with urllib.request.urlopen(
                f"{url}/api/_mock/harness-nonce", timeout=1,
            ) as resp:
                served_nonce = json.loads(resp.read().decode()).get("nonce")
            ready = True
            break
        except Exception:
            time.sleep(0.2)

    if not ready:
        proc.kill()
        out, err = proc.communicate(timeout=3)
        raise RuntimeError(
            "Mock server failed to start:\n"
            f"stdout: {out.decode()}\nstderr: {err.decode()}"
        )

    if served_nonce != nonce:
        proc.kill()
        raise RuntimeError(
            "Mock server identity check FAILED: the server answering on "
            f"{url} echoed nonce {served_nonce!r}, not this harness's "
            f"{nonce!r}. The probe reached a DIFFERENT server (port "
            "collision with another session, or a stale listener) — every "
            "test in this run would have executed against a stranger's code. "
            "Refusing to proceed."
        )

    return {
        "port": real_port,
        "url": url,
        "fixture_path": str(fixture_path),
        "events_path": str(events_path),
        "proc": proc,
    }


def stop_mock_server(state: dict) -> None:
    """Terminate the uvicorn process, falling back to ``kill`` after 5s."""
    proc = state.get("proc")
    if proc is None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=3)


# ── Browser lifecycle ───────────────────────────────────────────────────


def open_browser(url: str, *, wait_load: bool = True) -> None:
    """Open the shared ``agent-browser`` session at *url* and wait for
    network-idle.

    Tests reuse this single session across the module so we only pay the
    page-load cost once. ``wait_load=False`` skips the network-idle wait
    when callers know the page is intentionally lazy.

    A ``subprocess.TimeoutExpired`` from either call is deliberately NOT
    swallowed: a reopen that cannot finish inside the timeout means the
    daemon behind the session is degraded, and the caller must decide
    between failing and rotating to a fresh daemon
    (``rotate_browser_session``).
    """
    subprocess.run(
        ["agent-browser", "open", url],
        capture_output=True, timeout=30,
    )
    if wait_load:
        subprocess.run(
            ["agent-browser", "wait", "--load", "networkidle"],
            capture_output=True, timeout=30,
        )


def close_browser() -> None:
    """Close the shared ``agent-browser`` session (best-effort)."""
    try:
        subprocess.run(
            ["agent-browser", "close"],
            capture_output=True, timeout=5,
        )
    except subprocess.TimeoutExpired:
        pass  # a hung close is exactly the degraded-daemon case; the
        # caller rotates away and the idle timeout reaps the daemon


def rotate_browser_session() -> str:
    """Swap this process onto a fresh ``agent-browser`` session name and
    return it.

    Every session name owns its own daemon process, and there is no CLI
    verb that restarts a daemon in place — closing a SESSION leaves the
    (possibly degraded) daemon running. When a reopen hangs past its
    subprocess timeout (auto-s3him's late-sweep failure mode), renaming
    the session is the one in-band way to get a genuinely fresh daemon +
    Chromium. The old session is closed best-effort first; if that hangs
    too, the suite's ``AGENT_BROWSER_IDLE_TIMEOUT_MS`` backstop reaps it.
    """
    close_browser()
    name = os.environ.get("AGENT_BROWSER_SESSION", "pytest-l2b")
    base, sep, n = name.rpartition("~r")
    nxt = f"{base}~r{int(n) + 1}" if sep and n.isdigit() else f"{name}~r1"
    os.environ["AGENT_BROWSER_SESSION"] = nxt
    return nxt


# ── Eval helpers ────────────────────────────────────────────────────────


def _ab_eval_batch(js: str) -> Any:
    """Run a single ``agent-browser --json eval`` call and return the
    parsed result.

    The JS expression is wrapped in an IIFE so consecutive evals do not
    redeclare ``const``/``let`` against the same page context. The
    returned value is the unwrapped ``data.result`` from the
    ``--json`` envelope, or ``None`` if no parsable line was present.
    """
    wrapped = f"(() => {{ {js} }})()"
    _t0 = time.time()
    result = subprocess.run(
        ["agent-browser", "--json", "eval", wrapped],
        capture_output=True, text=True, timeout=10,
    )
    if os.environ.get("L2B_EVAL_TIMING_LOG"):
        with open(os.environ["L2B_EVAL_TIMING_LOG"], "a") as _f:
            _f.write(f"{time.time() - _t0:.2f}\n")
    stdout = result.stdout.strip()
    if not stdout:
        return None
    for line in reversed(stdout.split("\n")):
        try:
            parsed = json.loads(line)
            if isinstance(parsed, dict) and "data" in parsed:
                data = parsed["data"]
                if isinstance(data, dict) and "result" in data:
                    return data["result"]
                return data
            return parsed
        except json.JSONDecodeError:
            continue
    return None


def instrument_network() -> None:
    """Wrap window.fetch (idempotently) to track in-flight requests, so
    :func:`wait_dom_idle` can wait for network-idle. Must run BEFORE the
    navigation whose fetches we want to observe."""
    js = (
        "(() => { if (window.__l2bNet) return 'already';"
        " window.__l2bNet = true; window.__l2bInflight = 0;"
        " try { var of = window.fetch; window.fetch = function(){"
        "   window.__l2bInflight++;"
        "   return of.apply(this, arguments).finally(function(){"
        "     window.__l2bInflight = Math.max(0, window.__l2bInflight - 1); }); };"
        " } catch(e) {} return 'ok'; })()"
    )
    try:
        subprocess.run(
            ["agent-browser", "eval", js], capture_output=True, timeout=10,
        )
    except Exception:
        pass


def wait_dom_idle(idle_ms: int = 250, ceiling_ms: int = 1500) -> None:
    """Block until the page is quiescent — no in-flight fetches AND the DOM has
    been idle (no mutations) for *idle_ms* — or *ceiling_ms* elapses.

    The generic 'page finished loading and rendering' signal (Playwright's
    networkidle idea): a MutationObserver resets an idle timer on every DOM
    change, and the idle check only fires once ``window.__l2bInflight`` (set by
    :func:`instrument_network`) is zero. So this returns the instant the data
    fetch has resolved AND Alpine has stopped rendering it — it does NOT fire in
    the gap between init and the fetch's render, which a plain DOM-idle (or a
    fixed sleep) would race. A page that mutates or fetches forever hits the
    ceiling, never worse than the old blind sleep. (crypto pillar fixed-sleeps
    report, auto-0812-211339.)
    """
    js = (
        "(async () => { await new Promise(function(res){"
        " var t = null;"
        " function schedule(){ clearTimeout(t); t = setTimeout(check, %d); }"
        " function check(){ if ((window.__l2bInflight||0) <= 0)"
        "   { try{o.disconnect();}catch(e){} res(); } else { schedule(); } }"
        " var o;"
        " try {"
        "  o = new MutationObserver(schedule);"
        "  o.observe(document.documentElement,"
        "   {childList:true, subtree:true, attributes:true, characterData:true});"
        " } catch(e) {}"
        " schedule();"
        " setTimeout(function(){ try{o.disconnect();}catch(e){} res(); }, %d);"
        " }); return JSON.stringify({idle:true}); })()"
    ) % (idle_ms, ceiling_ms)
    try:
        subprocess.run(
            ["agent-browser", "--json", "eval", js],
            capture_output=True, text=True, timeout=ceiling_ms / 1000.0 + 5,
        )
    except Exception:
        pass


def _navigate_and_check(
    path: str,
    js_checks: str,
    wait_ms: int = 800,
) -> dict:
    """SPA-navigate to *path*, wait, run ONE batched JS eval, return dict.

    *js_checks* is JS source that mutates a pre-declared ``r`` object
    with check results; this helper wraps it in
    ``var r = {}; <checks>; return r;`` and submits via
    ``_ab_eval_batch``.
    """
    # Instrument fetch BEFORE navigating so wait_dom_idle can see the route's
    # own data fetch in-flight — a DOM-idle-only wait fires in the gap between
    # Alpine init and the fetch's render, which is what made several checks flap.
    instrument_network()
    nav_js = f"navigateTo('{path}')"
    subprocess.run(
        ["agent-browser", "eval", nav_js],
        capture_output=True, timeout=10,
    )
    # Wait for the DOM to stop mutating (the page has finished rendering,
    # including late async Alpine renders) instead of sleeping a fixed wait_ms
    # and hoping. Returns the instant the page settles — faster on every run,
    # and it waits for the RIGHT condition so it cannot lose a race a fixed
    # sleep would also lose. wait_ms is a generous ceiling. (crypto pillar
    # fixed-sleeps report, auto-0812-211339.)
    wait_dom_idle(ceiling_ms=max(wait_ms, 5000))

    full_js = f"var r = {{}}; {js_checks} return r;"
    return _ab_eval_batch(full_js) or {}


def _run_async_eval(js_expr: str) -> dict:
    """Run an async IIFE on the live page; return the parsed dict.

    The expression must be a self-contained IIFE that returns a
    ``JSON.stringify(...)`` string. Used by callers that need
    ``await`` for SPA route() resolution or Alpine.nextTick() chains.
    """
    result = subprocess.run(
        ["agent-browser", "--json", "eval", js_expr],
        capture_output=True, text=True, timeout=20,
    )
    stdout = result.stdout.strip()
    if not stdout:
        return {}
    for line in reversed(stdout.split("\n")):
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and "data" in parsed:
            data = parsed["data"]
            if isinstance(data, dict) and "result" in data:
                val = data["result"]
                if isinstance(val, str):
                    try:
                        return json.loads(val)
                    except (json.JSONDecodeError, TypeError):
                        pass
                if isinstance(val, dict):
                    return val
                return {}
    return {}


def _http_get(url: str) -> tuple[int, str]:
    """Tiny ``urllib`` GET wrapper returning ``(status, body)``.

    HTTPError is downgraded to ``(code, "")`` so callers can branch on
    status without try/except.
    """
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, ""
