"""Tests for the single DATA_ROOT that every data path routes through (auto-dnjn0).

DATA_ROOT and the module-level path constants derived from it are frozen at
import time, so each assertion imports in a fresh subprocess with a controlled
``AUTONOMY_DATA_ROOT``. Two things are pinned here:

* with the variable unset, ``DATA_ROOT`` equals the historical repo-local
  default — this bead moved no default (criterion 2);
* with the variable set to a tmp path, *every* exported data path resolves
  under it. This is the criterion that catches a missed call site: a site left
  on ``REPO_ROOT / "data"`` keeps pointing at the checkout and this fails
  (criterion 3).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run(code: str, *, data_root: str | None) -> str:
    """Import in a clean subprocess and return its stdout.

    ``AUTONOMY_DATA_ROOT`` is set (or explicitly removed) in the child env so
    the import-time constants are computed against a known value regardless of
    what the parent's test harness set.
    """
    # Strip AUTONOMY_DATA_ROOT AND every explicit per-store env var, so the
    # child computes each path from AUTONOMY_DATA_ROOT alone. Otherwise a
    # sibling suite that pins a store env for the whole xdist worker (the
    # dashboard hermetic-stores conftest sets AUTONOMY_ORGS_DIR,
    # DASHBOARD_IDENTITY_SESSION_DB, … directly in os.environ) leaks in and a
    # path follows that pin instead of the root under test.
    from tools.data_paths import STORE_MANIFEST
    _pins = {"AUTONOMY_DATA_ROOT"} | {s.env for s in STORE_MANIFEST if s.env}
    env = {
        k: v
        for k, v in __import__("os").environ.items()
        if k not in _pins
    }
    if data_root is not None:
        env["AUTONOMY_DATA_ROOT"] = data_root
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def test_data_root_default_is_repo_local_when_unset():
    """Criterion 2: unset AUTONOMY_DATA_ROOT ⇒ DATA_ROOT == REPO_ROOT / 'data'."""
    out = _run(
        "from tools.data_paths import DATA_ROOT, REPO_ROOT\n"
        "print(DATA_ROOT == REPO_ROOT / 'data')\n"
        "print(DATA_ROOT)",
        data_root=None,
    )
    matches, resolved = out.splitlines()
    assert matches == "True", resolved
    assert resolved == str(REPO_ROOT / "data")


# The full set of exported data paths that criterion 3 requires to move
# together. Each entry is (import statement, expression) evaluated in the child.
_EXPORTED_PATHS = [
    ("from tools.data_paths import DATA_ROOT", "DATA_ROOT"),
    ("from agents.workspace_manager import REPOS_DIR", "REPOS_DIR"),
    ("from agents.workspace_manager import WORKTREES_DIR", "WORKTREES_DIR"),
    ("from agents.workspace_manager import LOCAL_WORKSPACE_REPOS_DIR",
     "LOCAL_WORKSPACE_REPOS_DIR"),
    ("from agents.workspace_settings import DEFAULT_ARTIFACTS_ROOT",
     "DEFAULT_ARTIFACTS_ROOT"),
    ("from agents.backfill_runs import AGENT_RUNS_DIR", "AGENT_RUNS_DIR"),
    ("from agents.dispatch_db import DB_PATH", "DB_PATH"),
]


def test_every_exported_path_follows_the_env_var(tmp_path):
    """Criterion 3: with AUTONOMY_DATA_ROOT set, every exported path is under it.

    A single subprocess imports them all and prints one path per line so a
    missed call site is named in the failure, not hidden in an aggregate.
    """
    probe = tmp_path / "probe"
    imports = "\n".join(imp for imp, _ in _EXPORTED_PATHS)
    prints = "\n".join(f"print({expr})" for _, expr in _EXPORTED_PATHS)
    out = _run(imports + "\n" + prints, data_root=str(probe))
    resolved = out.splitlines()
    assert len(resolved) == len(_EXPORTED_PATHS)
    for (_, expr), path in zip(_EXPORTED_PATHS, resolved):
        assert path.startswith(str(probe)), f"{expr} did not follow AUTONOMY_DATA_ROOT: {path}"
