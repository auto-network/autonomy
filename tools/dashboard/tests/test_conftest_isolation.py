"""Regression for dashboard conftest's store isolation at import time."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
CONFTEST = Path(__file__).with_name("conftest.py")


def _run_conftest_probe(env: dict) -> dict:
    script = """
import json
import os
import runpy
from pathlib import Path

scope = runpy.run_path(%r)
print(json.dumps({
    "root": str(scope["_dashboard_repo_root"]()),
    "dispatch": os.environ.get("DISPATCH_DB"),
    "dashboard": os.environ.get("DASHBOARD_DB"),
    "orgs": os.environ.get("AUTONOMY_ORGS_DIR"),
    "graph_api": os.environ.get("GRAPH_API"),
    "refuse_guard": os.environ.get("AUTONOMY_REFUSE_REAL_DATA_FALLBACK"),
}))
""" % str(CONFTEST)
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_conftest_import_makes_stores_hermetic():
    """Importing the conftest redirects every store off the repo's data/
    (bead auto-5l5zt): DB env points under the per-worker tmp root, the
    live GRAPH_API pointer is dropped, and the refuse-real-data guard is
    on so any store the redirect misses fails loudly by name."""
    env = os.environ.copy()
    for key in ("DISPATCH_DB", "DASHBOARD_DB", "AUTONOMY_ORGS_DIR",
                "AUTONOMY_TESTS_USE_AMBIENT_STORES"):
        env.pop(key, None)
    env["GRAPH_API"] = "https://localhost:8080"  # the ambient hazard

    observed = _run_conftest_probe(env)

    assert observed["root"] == str(REPO_ROOT)
    tmp_root = tempfile.gettempdir()
    for key in ("dispatch", "dashboard", "orgs"):
        assert observed[key], f"{key} store env must be set hermetically"
        assert observed[key].startswith(tmp_root), (
            f"{key} store must live under tmp, got {observed[key]}")
        assert not observed[key].startswith(str(REPO_ROOT / "data")), (
            f"{key} store must never target repo data/")
    assert observed["graph_api"] is None, (
        "the live GRAPH_API pointer must be dropped for tests")
    assert observed["refuse_guard"] == "1"


def test_conftest_ambient_opt_out_preserves_env():
    """AUTONOMY_TESTS_USE_AMBIENT_STORES=1 (deliberate live-integration
    runs) leaves the ambient store env untouched."""
    env = os.environ.copy()
    env.pop("DISPATCH_DB", None)
    env.pop("DASHBOARD_DB", None)
    env["AUTONOMY_TESTS_USE_AMBIENT_STORES"] = "1"
    env["GRAPH_API"] = "https://localhost:8080"

    observed = _run_conftest_probe(env)

    assert observed["root"] == str(REPO_ROOT)
    assert observed["graph_api"] == "https://localhost:8080"
    # The read-only-workspace probe may still redirect on unwritable
    # checkouts; in this writable checkout nothing else sets these.
    assert observed["dispatch"] is None
    assert observed["dashboard"] is None
    assert (Path(observed["root"]) / "data").is_dir()
