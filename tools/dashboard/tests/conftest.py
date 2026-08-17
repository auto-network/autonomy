collect_ignore = [
    "test_agent_tool_calls.py",  # broken import: SessionState removed from session_monitor
    # Manual-run mock-server bootstrap, not a test module. Its import
    # PERMANENTLY patches SessionMonitor._check_tmux for the worker
    # process, corrupting any module collected after it (seen as
    # test_liveness_sweep failing only in combined parallel runs).
    "test_server.py",
]

"""
Shared test fixtures for dashboard functional tests.

Provides:
- mock_tmux: patches _tmux_session_exists to return True for test sessions
- mock_jsonl: creates a JSONL file with realistic Claude session entries
- test_db: creates a dashboard.db with test sessions pointing to JSONL
- test_app: boots the dashboard app against test data
- test_client: httpx AsyncClient for API tests
- browser: agent-browser helper for UI tests
"""
import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest
import httpx

_ISOLATED_TEST_ENVS = (
    "AGENT_BROWSER_SESSION",
    "AUTONOMY_ORGS_DIR",
    "DASHBOARD_AGENT_RUNS_DIR",
    "DASHBOARD_DB",
    "DASHBOARD_MOCK",
    "DASHBOARD_MOCK_EVENTS",
    "DISPATCH_DB",
    "GRAPH_API",
    "GRAPH_DB",
    "GRAPH_ORG",
)


@pytest.fixture(autouse=True)
def _restore_dashboard_env():
    """Restore common dashboard/graph env after each test.

    A number of dashboard suites still mutate ``os.environ`` directly instead
    of routing through ``monkeypatch``. Snapshot after higher-scope fixtures
    have set their test-local values, then restore after the test so later
    files on the same xdist worker do not inherit stray DB, mock, or browser
    settings.
    """
    snapshot = {key: os.environ.get(key) for key in _ISOLATED_TEST_ENVS}
    try:
        yield
    finally:
        store_env_changed = any(
            os.environ.get(key) != snapshot[key]
            for key in ("AUTONOMY_ORGS_DIR", "GRAPH_DB")
        )
        for key, value in snapshot.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if store_env_changed:
            # A test that re-pointed the graph stores leaves pooled
            # connections bound to ITS paths; the next test would read and
            # write through the stale pool instead of its own env.
            from tools.graph.db import GraphDB
            GraphDB.close_all_pooled()


# ── Read-only workspace auto-redirect ──────────────────────────────────
# Sub-session envs that mount /workspace/repo read-only still need DB
# fixtures that init_db() can open for write. Probe for write access and,
# on failure, redirect DISPATCH_DB/DASHBOARD_DB to a per-worker tmp copy.
# Writable envs short-circuit after the probe.
import os as _os
import shutil as _shutil
import tempfile as _tempfile
from pathlib import Path as _Path


def _dashboard_repo_root() -> _Path:
    """Return the checkout root, not the ``tools/`` package directory."""
    return _Path(__file__).resolve().parents[3]


def _configure_writable_dbs_if_readonly():
    repo = _dashboard_repo_root()
    data_dir = repo / "data"
    probe = data_dir / f".pytest-write-probe-{_os.getpid()}"
    try:
        probe.touch()
        probe.unlink()
        return
    except (OSError, PermissionError):
        pass
    worker = _os.environ.get("PYTEST_XDIST_WORKER", "master")
    tmp = _Path(_tempfile.gettempdir()) / f"pytest-dbs-{_os.getpid()}-{worker}"
    tmp.mkdir(parents=True, exist_ok=True)
    for name, env in (("dispatch.db", "DISPATCH_DB"),
                      ("dashboard.db", "DASHBOARD_DB")):
        src = data_dir / name
        dst = tmp / name
        if src.exists() and not dst.exists():
            _shutil.copy(src, dst)
        _os.environ.setdefault(env, str(dst))


_configure_writable_dbs_if_readonly()


# ── Hermetic stores (bead auto-5l5zt) ──────────────────────────────────
# The container's ambient env points GRAPH_API / GRAPH_DB / AUTONOMY_ORGS_DIR
# at the LIVE stores, so any test that touches a store without provisioning
# its own writes real data (the audit caught eager graph-source writes and
# ambient-store reads). Tests therefore get a hermetic per-worker data root
# for EVERY store in tools/data_paths.py::STORE_MANIFEST, and the
# refuse-real-data guard so a store this block ever misses fails loudly by
# name instead of silently touching the repo's data/.
#
# Org isolation is a real per-org TREE (AUTONOMY_ORGS_DIR), never a single
# pinned DB: pinning collapses org resolution to one database and makes
# every org-scope assertion pass regardless of the code under test (the
# manufactured-evidence tautology, methodology note 73bad14e).
#
# Escape hatch for deliberate live-integration runs only:
#   AUTONOMY_TESTS_USE_AMBIENT_STORES=1
def _configure_hermetic_stores():
    if _os.environ.get("AUTONOMY_TESTS_USE_AMBIENT_STORES") == "1":
        return
    from tools.data_paths import STORE_MANIFEST

    worker = _os.environ.get("PYTEST_XDIST_WORKER", "master")
    root = _Path(_tempfile.gettempdir()) / f"pytest-stores-{_os.getpid()}-{worker}"
    root.mkdir(parents=True, exist_ok=True)
    for store in STORE_MANIFEST:
        # The main graph store is deliberately NOT pinned: a GRAPH_DB pin
        # collapses explicit-org settings resolution (the manufactured-
        # evidence tautology, and under the fail-loud resolver every app
        # boot would conflict — the app's own bootstrap writes org rows).
        # Explicit-org ops route to the per-org tree; caller-scope
        # settings route to personal.db inside it; the few tests doing
        # general graph ops provide their own GRAPH_DB and are named
        # loudly by the refuse guard until they do.
        if store.key == "graph":
            continue
        if _os.environ.get(store.env, "").startswith(str(root)):
            continue
        dst = root / store.relative
        if store.kind == "dir":
            dst.mkdir(parents=True, exist_ok=True)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
        _os.environ[store.env] = str(dst)
    # The API pointer routes graph writes to the live dashboard; tests never
    # want that (suites that test the HTTP contract boot their own app).
    _os.environ.pop("GRAPH_API", None)
    _os.environ.pop("GRAPH_ORG", None)
    # NOTE: AUTONOMY_REFUSE_REAL_DATA_FALLBACK is deliberately NOT set here.
    # A module-level (import-time) os.environ set is process-global and leaks
    # to graph tests that share this xdist worker — flipping graph's resolver
    # to fail-loud and breaking the ones that legitimately exercise the legacy
    # real-data fallback (RealDataFallbackRefused). It is set per dashboard
    # test instead by the ``_refuse_real_data_fallback`` autouse fixture below,
    # so the fail-loud contract applies to dashboard tests only and restores
    # after each. See tools/dashboard/tests/conftest.py::_refuse_real_data_fallback.


_configure_hermetic_stores()


# ── Default EventBus snapshot redirect ─────────────────────────────────
# Prevent any test that boots the server (TestClient lifespan or uvicorn
# subprocess) from reading or writing the real repo's data/event_bus.state.
# Per-fixture redirects (e.g. test_app, setup_env) override this with a
# tmp_path-scoped value where stricter isolation is needed.
def _set_default_event_bus_state_path():
    if _os.environ.get("DASHBOARD_EVENT_BUS_STATE"):
        return
    worker = _os.environ.get("PYTEST_XDIST_WORKER", "master")
    tmp = _Path(_tempfile.gettempdir()) / f"pytest-event-bus-state-{_os.getpid()}-{worker}"
    tmp.mkdir(parents=True, exist_ok=True)
    _os.environ["DASHBOARD_EVENT_BUS_STATE"] = str(tmp / "event_bus.state")


_set_default_event_bus_state_path()


@pytest.fixture(autouse=True)
def _refuse_real_data_fallback(monkeypatch):
    """Dashboard tests must never fall through to the operator's real graph
    data — resolution should raise rather than read data/graph.db. Set that
    fail-loud flag PER TEST via monkeypatch (auto-restored) instead of a
    process-global os.environ set: the latter leaks to graph tests that share
    this xdist worker (or a serial run) and breaks the ones that legitimately
    exercise the legacy real-data fallback. Applies only under this conftest
    (dashboard tests), so graph tests on the same worker are unaffected.
    """
    monkeypatch.setenv("AUTONOMY_REFUSE_REAL_DATA_FALLBACK", "1")


@pytest.fixture(autouse=True)
def _isolate_schema_registry_global():
    """Snapshot + restore the process-global schema registry around every
    dashboard test.

    ``tools.graph.schemas.registry.SCHEMAS`` / ``UPCONVERTERS`` are mutable
    module-level dicts. Tests that register permissive/stub schemas (directly
    or by importing a plugin's actions module) mutate them in place; without
    cleanup the extra registrations leak to later tests on the same xdist
    worker, which then resolve the wrong validator and fail in a shifting,
    hard-to-reproduce way (e.g. coordinator_board dispatch after a mediator
    test). Restoring here means no dashboard test can leave the registry
    dirty for the next one — including tests in other packages that only
    isolate the registry for themselves.
    """
    from tools.graph.schemas import registry as _reg
    schemas_snap = dict(_reg.SCHEMAS)
    upcon_snap = dict(_reg.UPCONVERTERS)
    try:
        yield
    finally:
        _reg.SCHEMAS.clear()
        _reg.SCHEMAS.update(schemas_snap)
        _reg.UPCONVERTERS.clear()
        _reg.UPCONVERTERS.update(upcon_snap)


@pytest.fixture(autouse=True)
def _contain_shared_db_reload_leak():
    """Undo cross-test contamination from fixtures that ``importlib.reload``
    the shared ``dashboard_db`` / ``dispatch_db`` singletons to inject a test
    DB path.

    Both modules resolve their DB path at IMPORT time
    (``dashboard_db._DB_PATH = resolve_store("dashboard")``;
    ``dispatch_db.DB_PATH = Path(os.environ["DISPATCH_DB"] ...)``), so tests
    reload them after setting the env to repoint at a tmp DB. Many such
    fixtures pop the env on teardown but never reload the module back, leaving
    the process-global singleton resolved to a now-deleted tmp path. Under
    ``-n 8 --dist loadfile`` that poisons every later test on the same worker
    (they read the stale path and fail) — a shifting, hard-to-reproduce set of
    failures. After each test, if a module's cached path no longer matches what
    the current environment resolves to, reload it back to a clean baseline.
    """
    yield
    import importlib
    try:
        from tools.data_paths import resolve_store
        from tools.dashboard.dao import dashboard_db as _ddb
        if getattr(_ddb, "_DB_PATH", None) != resolve_store("dashboard"):
            importlib.reload(_ddb)
    except Exception:
        pass
    try:
        from agents import dispatch_db as _disp
        expected = _Path(
            _os.environ.get("DISPATCH_DB", str(_disp.REPO_ROOT / "data" / "dispatch.db"))
        )
        if getattr(_disp, "DB_PATH", None) != expected:
            importlib.reload(_disp)
    except Exception:
        pass


# ── Per-worker agent-browser session fallback ──────────────────────────
# The ROOT conftest.py gives each dashboard test module its own browser
# session (pytest-<worker>-<module>-<hash>) via a module-scoped autouse
# fixture and closes it at module teardown. This import-time default only
# covers the gaps outside any module fixture — collection-time helpers,
# pytest_sessionfinish, and non-dashboard tests — so stray agent-browser
# calls never land on the operator's default daemon session. Subprocess
# calls inherit os.environ, so both layers cover every helper
# (l2b_harness, per-file ab()/ab_raw() wrappers, BrowserHelper).
def _isolate_agent_browser_session():
    # agent-browser installs under nvm's node bin, which non-login shells
    # do not have on PATH — a run launched outside a login shell then
    # errors EVERY browser class at fixture time with FileNotFoundError
    # (measured: 79 of 83 errors in one host run). Wire it up here so
    # the suite works the same from any shell.
    import shutil as _shutil, glob as _glob
    if _shutil.which("agent-browser") is None:
        for _cand in sorted(_glob.glob(
                _os.path.expanduser("~/.nvm/versions/node/*/bin"))):
            if (_os.path.exists(_os.path.join(_cand, "agent-browser"))):
                _os.environ["PATH"] = _cand + _os.pathsep + _os.environ["PATH"]
                break
    if _os.environ.get("AGENT_BROWSER_SESSION"):
        return
    worker = _os.environ.get("PYTEST_XDIST_WORKER", "master")
    _os.environ["AGENT_BROWSER_SESSION"] = f"pytest-{_os.getpid()}-{worker}"
    # Test-session daemons must not outlive the run by the default hour:
    # a day of suite runs accumulated 77 idle daemons (auto-s3him's
    # measured leak), and each consecutive run degraded under the pile —
    # the "late-file load" failures' substrate. Two minutes covers any
    # legitimate between-command gap in a test.
    _os.environ.setdefault("AGENT_BROWSER_IDLE_TIMEOUT_MS", "120000")


_isolate_agent_browser_session()


def pytest_sessionfinish(session, exitstatus):
    """Reap this worker's browser daemon at exit (auto-s3him).

    The idle timeout above is the backstop for sessions test code names
    itself; this closes the worker's own session deterministically so
    back-to-back runs start clean instead of inheriting daemons.
    """
    name = _os.environ.get("AGENT_BROWSER_SESSION", "")
    if not name.startswith("pytest-"):
        return
    import subprocess
    try:
        subprocess.run(["agent-browser", "close", "--session", name],
                       capture_output=True, timeout=15)
    except Exception:
        pass  # reaping is best-effort; the idle timeout finishes the job


# ── Repo-data write redirects ──────────────────────────────────────────
# session_trace.py appends launch-phase JSONL under data/session-traces and
# the launch path creates run dirs under data/agent-runs by default; tests
# exercising create/lifecycle would litter the real repo. Point both at
# per-worker tmp dirs (uvicorn subprocesses inherit them). Individual
# fixtures still override DASHBOARD_AGENT_RUNS_DIR where they need to
# assert on run-dir contents.
def _isolate_repo_data_writes():
    worker = _os.environ.get("PYTEST_XDIST_WORKER", "master")
    for env, name in (
        ("DASHBOARD_TRACE_DIR", "session-traces"),
        ("DASHBOARD_AGENT_RUNS_DIR", "agent-runs"),
    ):
        if _os.environ.get(env):
            continue
        tmp = _Path(_tempfile.gettempdir()) / f"pytest-{name}-{_os.getpid()}-{worker}"
        tmp.mkdir(parents=True, exist_ok=True)
        _os.environ[env] = str(tmp)


_isolate_repo_data_writes()


def pytest_sessionfinish(session, exitstatus):
    """Close this worker's namespaced browser session so Chromium
    instances do not accumulate across pytest runs."""
    name = _os.environ.get("AGENT_BROWSER_SESSION", "")
    if name.startswith("pytest-"):
        subprocess.run(["agent-browser", "close"], capture_output=True, timeout=10)


# ── JSONL Fixture ──────────────────────────────────────────────────────

MOCK_ENTRIES = [
    {
        "type": "human",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": "Hello, can you help me with the session cards?"}]
        },
        "timestamp": "2026-03-24T12:00:00Z"
    },
    {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Sure! Let me look at the session card code."},
                {
                    "type": "tool_use",
                    "id": "tu_001",
                    "name": "Read",
                    "input": {"file_path": "/workspace/repo/tools/dashboard/templates/pages/sessions.html"}
                }
            ]
        },
        "timestamp": "2026-03-24T12:00:05Z"
    },
    {
        "type": "human",
        "message": {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": "tu_001",
                "content": "<!-- sessions template -->\n<div x-data=\"sessionsPage()\">\n  <h2>Active Sessions</h2>\n</div>"
            }]
        },
        "timestamp": "2026-03-24T12:00:06Z"
    },
    {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "I can see the sessions template. The cards use Alpine.js with the sessionsPage() component."}]
        },
        "timestamp": "2026-03-24T12:00:10Z"
    },
]

MOCK_SESSIONS = [
    {
        "tmux_name": "auto-test-designer",
        "type": "container",
        "project": "autonomy",
        "label": "Test Designer — card redesign",
        "role": "designer",
        "last_message": "Sure! Let me look at the session card code.",
        "entry_count": 4,
        "context_tokens": 250000,
    },
    {
        "tmux_name": "auto-test-validator",
        "type": "container",
        "project": "autonomy",
        "label": "Test Validator",
        "role": "reviewer",
        "last_message": "auto-f4p4 validated PASS. All 8 gaps verified.",
        "entry_count": 200,
        "context_tokens": 100000,
    },
    {
        "tmux_name": "auto-test-coordinator",
        "type": "container",
        "project": "autonomy",
        "label": "Session Coordinator",
        "role": "coordinator",
        "last_message": "Fleet status: 4 active sessions, all working.",
        "entry_count": 1500,
        "context_tokens": 300000,
    },
    {
        "tmux_name": "host-test-host",
        "type": "host",
        "project": "autonomy",
        "label": "Host: merge recovery",
        "role": "",
        "last_message": "Dolt server restarted.",
        "entry_count": 50,
        "context_tokens": 30000,
    },
    {
        "tmux_name": "chatwith-should-be-hidden",
        "type": "container",
        "project": "autonomy",
        "label": "",
        "role": "",
        "last_message": "orphan chatwith session",
        "entry_count": 5,
        "context_tokens": 10000,
    },
]


@pytest.fixture
def mock_jsonl(tmp_path):
    """Create a JSONL file with realistic Claude session entries."""
    jsonl_path = tmp_path / "sessions" / "test-uuid" / "test.jsonl"
    jsonl_path.parent.mkdir(parents=True)
    with open(jsonl_path, "w") as f:
        for entry in MOCK_ENTRIES:
            f.write(json.dumps(entry) + "\n")
    return str(jsonl_path)


@pytest.fixture
def test_db(tmp_path, mock_jsonl):
    """Create a dashboard.db with test sessions."""
    db_path = tmp_path / "dashboard.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""CREATE TABLE IF NOT EXISTS tmux_sessions (
        tmux_name TEXT PRIMARY KEY, session_uuid TEXT, graph_source_id TEXT,
        type TEXT NOT NULL, project TEXT NOT NULL, jsonl_path TEXT,
        bead_id TEXT, created_at REAL NOT NULL, is_live INTEGER DEFAULT 1,
        file_offset INTEGER DEFAULT 0, last_activity REAL,
        last_message TEXT DEFAULT '', entry_count INTEGER DEFAULT 0,
        context_tokens INTEGER DEFAULT 0, label TEXT DEFAULT '',
        topics TEXT DEFAULT '[]', role TEXT DEFAULT '',
        nag_enabled INTEGER DEFAULT 0, nag_interval INTEGER DEFAULT 15,
        nag_message TEXT DEFAULT '', nag_last_sent REAL DEFAULT 0,
        dispatch_nag INTEGER DEFAULT 0,
        resolution_dir TEXT, session_uuids TEXT DEFAULT '[]',
        curr_jsonl_file TEXT
    )""")
    now = time.time()
    for s in MOCK_SESSIONS:
        jsonl = mock_jsonl if s["tmux_name"] == "auto-test-designer" else None
        conn.execute(
            """INSERT INTO tmux_sessions
            (tmux_name, type, project, created_at, is_live, last_message,
             entry_count, context_tokens, label, role, jsonl_path, session_uuid, last_activity)
            VALUES (?,?,?,?,1,?,?,?,?,?,?,?,?)""",
            (s["tmux_name"], s["type"], s["project"], now, s["last_message"],
             s["entry_count"], s["context_tokens"], s["label"], s["role"],
             jsonl, f"uuid-{s['tmux_name']}", now)
        )
    conn.commit()
    conn.close()
    return str(db_path)


@pytest.fixture
def mock_tmux():
    """Patch tmux session existence check so test sessions stay alive."""
    test_sessions = {s["tmux_name"] for s in MOCK_SESSIONS}

    def fake_check_tmux(name):
        return name in test_sessions

    with patch("tools.dashboard.session_monitor.SessionMonitor._check_tmux", staticmethod(fake_check_tmux)):
        yield


@pytest.fixture
def shipped_settings_orgs(test_app, monkeypatch):
    """Populate shipped-workspace Settings into the app's orgs/ dir.

    Dashboard API handlers that render the workspace registry read from
    ``autonomy.workspace#1`` + ``autonomy.org#1`` Settings; tests need
    those populated so ``load_workspaces`` returns the same registry the
    live dashboard shows.

    Depends on ``test_app`` deliberately: ``test_app`` sets
    ``AUTONOMY_ORGS_DIR`` to its own per-test empty orgs dir (to keep the
    sign-in gate fail-open), and if this fixture populated a DIFFERENT
    dir, ``test_app``'s later assignment would clobber it and the
    endpoint would read the empty dir — the whole registry coming back
    empty. Populating into the dir ``test_app`` already established means
    the app reads what we seed.
    """
    import os
    from pathlib import Path
    from tools.graph.db import GraphDB
    from agents.tests.conftest import (
        SHIPPED_PROJECTS_YAML,
        populate_workspaces_from_yaml,
    )

    orgs_dir = Path(os.environ["AUTONOMY_ORGS_DIR"])
    orgs_dir.mkdir(parents=True, exist_ok=True)
    GraphDB.close_all_pooled()
    monkeypatch.delenv("GRAPH_DB", raising=False)
    populate_workspaces_from_yaml(SHIPPED_PROJECTS_YAML, orgs_dir)
    GraphDB.close_all_pooled()
    try:
        yield orgs_dir
    finally:
        GraphDB.close_all_pooled()


@pytest.fixture
def test_app(test_db, mock_tmux, tmp_path):
    """Boot the dashboard app against test data with tmux mocked."""
    os.environ["DASHBOARD_DB"] = test_db
    # Redirect EventBus snapshot path so the TestClient lifespan never
    # reads or writes the real repo's data/event_bus.state.
    os.environ["DASHBOARD_EVENT_BUS_STATE"] = str(tmp_path / "event_bus.state")
    # Hermetic personal scope: the repo checkout carries the operator's real
    # data/orgs/personal.db, whose enrolled identity flips the sign-in gate
    # to enforcing and 401s every gated page these tests fetch. An empty
    # orgs dir keeps the gate in its unenrolled fail-open state; tests that
    # exercise the gate itself enroll their own identity into this dir.
    orgs_dir = tmp_path / "orgs"
    orgs_dir.mkdir(exist_ok=True)
    os.environ["AUTONOMY_ORGS_DIR"] = str(orgs_dir)
    # Reload DAO to pick up new DB path
    import importlib
    from tools.dashboard.dao import dashboard_db as db_mod
    importlib.reload(db_mod)
    # Must reload server after DAO to pick up new connection
    from tools.dashboard import server
    importlib.reload(server)
    return server.app


@pytest.fixture
def test_client(test_app):
    """Sync HTTP client for API testing."""
    from starlette.testclient import TestClient
    with TestClient(test_app) as client:
        yield client


# ── Agent Browser Helper ──────────────────────────────────────────────

class BrowserHelper:
    """Wrapper around agent-browser CLI for UI testing."""

    def __init__(self, base_url):
        self.base_url = base_url
        self._started = False

    def _run(self, *args, timeout=10):
        result = subprocess.run(
            ["agent-browser"] + list(args),
            capture_output=True, text=True, timeout=timeout
        )
        return result.stdout + result.stderr

    def open(self, path):
        url = f"{self.base_url}{path}"
        self._run("open", url, "--ignore-https-errors")
        self._started = True
        return self

    def set_viewport(self, width=390, height=844):
        self._run("set", "viewport", str(width), str(height))
        return self

    def screenshot(self, annotate=False):
        args = ["screenshot"]
        if annotate:
            args.append("--annotate")
        output = self._run(*args)
        # Extract path from output
        for line in output.split("\n"):
            if "/tmp/screenshots/" in line:
                path = line.split("/tmp/screenshots/")[1].split()[0]
                return f"/tmp/screenshots/{path}"
        return None

    def snapshot(self):
        return self._run("snapshot", "-i")

    def click(self, ref):
        return self._run("click", ref)

    def eval_js(self, js):
        return self._run("eval", js)

    def close(self):
        if self._started:
            self._run("close")
            self._started = False


@pytest.fixture
def browser():
    """Agent-browser instance. Tests must start their own server."""
    from tools.dashboard.tests._xdist import worker_test_port
    b = BrowserHelper(f"http://localhost:{worker_test_port(8082)}")
    yield b
    b.close()
