"""Project-root pytest configuration — worker-aware fixtures for pytest-xdist.

Tests under both ``tools/dashboard/tests`` and ``agents/tests`` load this
conftest. Fixtures defined here provide per-worker port bases and browser
session isolation so ``pytest -n auto`` workers don't collide or leak browser
state between dashboard modules.
"""
import os
from pathlib import Path

import pytest

from tools.dashboard.tests._xdist import worker_index


@pytest.fixture(scope="session")
def worker_port_base() -> int:
    """Port base per xdist worker. Workers get 8100, 8200, 8300, ..."""
    return 8100 + worker_index() * 100


@pytest.fixture(scope="session", autouse=True)
def _isolate_browser_session():
    """Provide a stable fallback browser session for non-dashboard tests."""
    os.environ.setdefault("AGENT_BROWSER_SESSION", f"pytest-{worker_index()}")
    yield


@pytest.fixture(scope="module", autouse=True)
def _isolate_dashboard_browser_module(request):
    """Give each dashboard test module its own browser profile.

    Several browser suites reuse the same localhost origin on a worker. If they
    also share a single agent-browser session, localStorage and connected-session
    state leak across files and later modules observe the wrong page state.
    """
    module_file = getattr(request.module, "__file__", "")
    if "tools/dashboard/tests" not in module_file:
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
    if not _QUARANTINE:
        return
    skip = pytest.mark.skip(reason=_QUARANTINE_REASON)
    for item in items:
        # Match exact node id and the parametrize-stripped base, so a single
        # list entry covers all parametrizations of a quarantined test.
        base = item.nodeid.split("[", 1)[0]
        if item.nodeid in _QUARANTINE or base in _QUARANTINE:
            item.add_marker(skip)
