"""Regression for dashboard conftest's read-only workspace detection."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
CONFTEST = Path(__file__).with_name("conftest.py")


def test_collection_probe_targets_repo_data_without_db_env_side_effects():
    """A writable checkout must not be mistaken for a read-only workspace."""
    env = os.environ.copy()
    env.pop("DISPATCH_DB", None)
    env.pop("DASHBOARD_DB", None)
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
    observed = json.loads(completed.stdout.strip().splitlines()[-1])

    assert observed == {
        "root": str(REPO_ROOT),
        "dispatch": None,
        "dashboard": None,
    }
    assert (Path(observed["root"]) / "data").is_dir()
