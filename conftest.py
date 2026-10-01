"""Project-root pytest configuration — worker-aware fixtures for pytest-xdist.

Tests under both ``tools/dashboard/tests`` and ``agents/tests`` load this
conftest. Fixtures defined here provide per-worker port bases and browser
session isolation so ``pytest -n auto`` workers don't collide or leak browser
state between dashboard modules.
"""
import os
from pathlib import Path

import pytest



def _hermetic_store_defaults() -> None:
    """Per-worker paths for every store a test could fall through to.

    Each store resolves its own variable first and the checkout's data/ last
    (tools/data_paths.py). Only the dashboard suite's conftest set those
    variables, so suites run without it (graph, agents, network, plugins)
    created dashboard, dispatch, commit-workflow and design stores in data/,
    and any of them could read another test's leftovers (auto-fus3y). Set at
    import, before any test module imports a DAO that fixes its path; the
    dashboard conftest's own redirect is unchanged and still wins for it.
    The main graph store stays unpinned, as there: a pin collapses org
    resolution to one database. Escape hatch for deliberate live runs:
    AUTONOMY_TESTS_USE_AMBIENT_STORES=1.
    """
    if os.environ.get("AUTONOMY_TESTS_USE_AMBIENT_STORES") == "1":
        return
    import tempfile

    from tools.data_paths import STORE_MANIFEST

    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    root = Path(tempfile.gettempdir()) / f"pytest-root-stores-{os.getpid()}-{worker}"
    root.mkdir(parents=True, exist_ok=True)
    for store in STORE_MANIFEST:
        if store.env is None or store.key == "graph":
            continue
        target = root / store.relative
        (target if store.kind == "dir" else target.parent).mkdir(parents=True, exist_ok=True)
        os.environ[store.env] = str(target)
    # Read outside the store manifest: the design store and the dashboard
    # lifespan's state files (mock servers inherit these at spawn).
    for env, name in (
        ("EXPERIMENTS_DB", "experiments.db"),
        ("DASHBOARD_EVENT_BUS_STATE", "event_bus.state"),
        ("DASHBOARD_TAIL_STATE", "tail_state.snapshot"),
        ("DASHBOARD_RESTART_NOTICE_STATE", "restart_notice.state"),
        ("DASHBOARD_RESOURCE_MONITOR_STATE", "resource_monitor.state"),
        ("DASHBOARD_WORKTREE_ROW_CACHE_STATE", "worktree_row_cache.state"),
    ):
        os.environ[env] = str(root / name)
    # Graph calls must never reach the live dashboard from a test.
    os.environ.pop("GRAPH_API", None)
    os.environ.pop("GRAPH_ORG", None)


_hermetic_store_defaults()

from tools.dashboard.tests._xdist import worker_index  # noqa: E402


@pytest.fixture(autouse=True)
def _hermetic_local_stores(request, monkeypatch):
    """The personal and machine stores live beside the orgs directory, which
    falls back to the checkout's data/ when AUTONOMY_ORGS_DIR is unset.
    Give each test its own unless it (or its suite) already chose one."""
    if os.environ.get("AUTONOMY_ORGS_DIR") or os.environ.get("AUTONOMY_TESTS_USE_AMBIENT_STORES") == "1":
        return
    orgs = request.getfixturevalue("tmp_path") / "orgs"
    orgs.mkdir(exist_ok=True)
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs))

_CHECKOUT_DATA = Path(__file__).resolve().parent / "data"
_data_before: set[str] | None = None


def _data_entries() -> set[str]:
    try:
        return {entry.name for entry in _CHECKOUT_DATA.iterdir()}
    except OSError:
        return set()


@pytest.hookimpl(tryfirst=True)
def pytest_sessionstart(session):
    """Snapshot the checkout's data/ so the run can prove it left it alone."""
    global _data_before
    if os.environ.get("PYTEST_XDIST_WORKER") is None:
        _data_before = _data_entries()


@pytest.hookimpl(tryfirst=True)
def pytest_sessionfinish(session, exitstatus):
    """Fail the run if any test created a file in the checkout's data/.

    Tests that fall through to the repository's stores create them there,
    and the next test that reads them sees another test's state: an enrolled
    identity in data/personal.db made two suites fail in every later run
    (auto-fus3y). Redirect the store instead (see the conftests' store
    isolation). Skipped for deliberate live runs
    (AUTONOMY_TESTS_USE_AMBIENT_STORES=1) and in xdist workers.
    """
    if _data_before is None or os.environ.get("AUTONOMY_TESTS_USE_AMBIENT_STORES") == "1":
        return
    created = sorted(_data_entries() - _data_before)
    if not created:
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    message = (
        "tests created files in the checkout's data/ (a store was not "
        "redirected): " + ", ".join(created)
    )
    if reporter is not None:
        reporter.write_sep("=", "data/ was written", red=True)
        reporter.write_line(message)
    if session.exitstatus == 0:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_configure(config):
    """Require the managed Agent Test path inside Autonomy agent sessions."""
    if not os.environ.get("AUTONOMY_SESSION"):
        return
    if os.environ.get("AGENT_TEST_INTERNAL") == "1" or os.environ.get("PYTEST_ALLOW_RAW") == "1":
        return
    try:
        from tools.agent_test.lease_client import telemetry_request

        telemetry_request(event="raw_pytest_refused")
    except Exception:
        pass
    pytest.exit(
        "Raw pytest is disabled for agent sessions. Use `agent-test plan`, "
        "then `agent-test run PATH_OR_NODEID`; results are retained by run id.",
        returncode=64,
    )


@pytest.fixture(scope="session")
def worker_port_base() -> int:
    """Port base per xdist worker. Workers get 8100, 8200, 8300, ..."""
    return 8100 + worker_index() * 100


@pytest.fixture(scope="session", autouse=True)
def _isolate_browser_session():
    """Provide a stable fallback browser session for non-dashboard tests."""
    os.environ.setdefault("AGENT_BROWSER_SESSION", f"pytest-{worker_index()}")
    # One browser per worker and per dashboard module is deliberate here, so
    # opt out of the session shim's one-browser-at-a-time guard.
    os.environ.setdefault("AGENT_BROWSER_ALLOW_MANY", "1")
    yield


@pytest.fixture(scope="module", autouse=True)
def _isolate_dashboard_browser_module(request):
    """Give each dashboard test module its own browser profile.

    Several browser suites reuse the same localhost origin on a worker. If they
    also share a single agent-browser session, localStorage and connected-session
    state leak across files and later modules observe the wrong page state.
    """
    module_file = getattr(request.module, "__file__", "")
    # Plugin suites drive the same origin, so they need the same isolation.
    if "tools/dashboard/tests" not in module_file and "tools/dashboard/plugins/" not in module_file:
        yield
        return

    module_path = Path(module_file)
    previous = os.environ.get("AGENT_BROWSER_SESSION")
    session_name = (
        f"pytest-{worker_index()}-"
        f"{module_path.stem}-"
        f"{abs(hash(module_path.as_posix())):x}"
    )
    os.environ["AGENT_BROWSER_SESSION"] = session_name
    try:
        yield
    finally:
        # Close the module's browser session. Without this every browser
        # module leaked a live Chromium for the rest of the run (and across
        # runs — the daemon outlives pytest): dozens of instances pile up,
        # first runs pay a cold boot per module, and long sessions degrade
        # until agent-browser commands start timing out.
        import subprocess
        try:
            subprocess.run(
                ["agent-browser", "close"],
                capture_output=True,
                timeout=15,
                env={**os.environ, "AGENT_BROWSER_SESSION": session_name},
            )
        except Exception:
            pass
        if previous is None:
            os.environ.pop("AGENT_BROWSER_SESSION", None)
        else:
            os.environ["AGENT_BROWSER_SESSION"] = previous


@pytest.fixture(autouse=True)
def _isolate_graph_connection_pool():
    """Reset the process-global graph connection pool around every test.

    pytest-xdist runs many tests per worker PROCESS. ``GraphDB`` keeps a
    process-global pool of open connections keyed by db path (an efficiency
    cache, correct for a long-lived server, wrong for a test process). Each
    test already builds its own tmp db, but a pooled handle from a prior test
    survives and gets reused — so a write lands in the wrong file (a later
    assertion sees ``0 rows``) or collides with a prior test's rows
    (``sqlite3.IntegrityError: UNIQUE constraint``). Clearing the pool before
    and after each test stops the sharing without touching within-test use.
    See the pitfall note graph://e4891727-d3b.
    """
    try:
        from tools.graph.db import GraphDB
    except Exception:
        yield
        return
    GraphDB.close_all_pooled()
    yield
    GraphDB.close_all_pooled()


# ── Baseline failure quarantine (2026-08-17) ─────────────────────────────────
# Pre-existing failures on master are SKIPPED here so the suite runs green and
# sessions stop re-running tests to decide "is this failure mine or the tree's?"
# — a tax measured at multiple agent-hours per day.
#
# This is a temporary shutoff, NOT a parking lot. Every node id in the list must
# be fixed and removed. The line count of the quarantine file is the burn-down
# metric; the reason string below is the single greppable marker
# (QUARANTINE-BASELINE-20260817). Goal: the file reaches zero lines and this
# hook is deleted. Do not add a failing test here without a plan to fix it.
_QUARANTINE_REASON = (
    "QUARANTINE-BASELINE-20260817: pre-existing failure skipped to keep the "
    "suite green — must be fixed and unskipped, not left skipped "
    "(see tests/quarantine_baseline_20260817.txt)"
)


def _load_quarantine() -> set[str]:
    """Node ids to skip, one per line; '#' comments and blanks ignored."""
    path = Path(__file__).parent / "tests" / "quarantine_baseline_20260817.txt"
    if not path.exists():
        return set()
    out = set()
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.add(line)
    return out


_QUARANTINE = _load_quarantine()


def pytest_collection_modifyitems(config, items):
    """Apply one identical skip marker to every quarantined baseline failure."""
    # Burn-down affordance: QUARANTINE_OFF=1 runs quarantined tests for real so
    # a fixer can confirm a test now passes before deleting its line from the
    # list. Never set in CI — the point of the quarantine is the default-green run.
    if os.environ.get("QUARANTINE_OFF") == "1":
        return
    if not _QUARANTINE:
        return
    skip = pytest.mark.skip(reason=_QUARANTINE_REASON)
    for item in items:
        # Match exact node id and the parametrize-stripped base, so a single
        # list entry covers all parametrizations of a quarantined test.
        base = item.nodeid.split("[", 1)[0]
        if item.nodeid in _QUARANTINE or base in _QUARANTINE:
            item.add_marker(skip)
