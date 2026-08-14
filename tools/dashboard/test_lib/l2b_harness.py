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
import subprocess
import sys
import time
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
    fixture_path = tmp_path / "fixtures.json"
    fixture_path.write_text(json.dumps(fixture_data, indent=2))

    events_path = tmp_path / "events.jsonl"
    events_path.write_text("")

    # Kill any stale listener squatting this port BEFORE spawning. Without
    # this, our uvicorn silently fails to bind while the readiness probe
    # below answers from the squatter — which then serves the OLD code and
    # old fixture for the whole module (observed by auto-0708-153344 as a
    # phantom 'pre-existing' failure).
    subprocess.run(
        ["pkill", "-f", f"uvicorn.*{port}"],
        capture_output=True, timeout=3,
    )
    time.sleep(0.5)

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
            "--host", "127.0.0.1",
            "--port", str(port),
            "--log-level", "warning",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # 30s window: uvicorn imports the full server module; under 8-way xdist
    # contention plus per-module Chromium cold boots, 8s is routinely
    # exceeded on a loaded machine.
    deadline = time.time() + 45
    ready = False
    while time.time() < deadline:
        try:
            import urllib.request
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/dao/active_sessions",
                timeout=1,
            )
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

    return {
        "port": port,
        "url": f"http://127.0.0.1:{port}",
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
    result = subprocess.run(
        ["agent-browser", "--json", "eval", wrapped],
        capture_output=True, text=True, timeout=10,
    )
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
    nav_js = f"navigateTo('{path}')"
    subprocess.run(
        ["agent-browser", "eval", nav_js],
        capture_output=True, timeout=10,
    )
    time.sleep(wait_ms / 1000)

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
