"""auto-493mx: the deploy-time import-closure guard.

The tool imports the entry points under a deployed root; a missing in-repo
module its closure needs surfaces as a ModuleNotFoundError and blocks the
deploy. Driven as a SUBPROCESS so each case gets a clean interpreter (no
sys.modules pollution, no namespace-package merge with the test's own tree).
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "deploy" / "check_import_closure.py"


def _mod(root: Path, dotted: str, body: str = "") -> None:
    path = root.joinpath(*dotted.split(".")).with_suffix(".py")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body))


def _run(root: Path, *entries: str, cwd=None, clean_env=False):
    env = None
    if clean_env:
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    return subprocess.run(
        [sys.executable, str(TOOL), str(root), *entries],
        capture_output=True, text=True, cwd=cwd, env=env,
    )


def test_complete_closure_passes(tmp_path):
    # A namespace-style tree (no __init__.py) with a relative import, exactly
    # like the real registry — the tool resolves it via real import machinery.
    _mod(tmp_path, "pkg.entry", "from . import helper\nimport os\n")
    _mod(tmp_path, "pkg.helper", "VALUE = 1\n")
    r = _run(tmp_path, "pkg.entry")
    assert r.returncode == 0, r.stderr
    assert "import-closure OK" in r.stdout


def test_missing_closure_module_blocks(tmp_path):
    _mod(tmp_path, "pkg.entry", "from . import helper\n")
    # pkg.helper deliberately absent.
    r = _run(tmp_path, "pkg.entry")
    assert r.returncode == 1
    assert "DEPLOY BLOCKED" in r.stderr
    # `from . import helper` (missing) raises ImportError naming the package.
    assert "pkg" in r.stderr


def test_relative_deep_import_is_followed(tmp_path):
    # A transitive relative import must be walked: entry -> a -> (missing) b.
    _mod(tmp_path, "pkg.entry", "from . import a\n")
    _mod(tmp_path, "pkg.a", "from . import b\n")
    r = _run(tmp_path, "pkg.entry")
    assert r.returncode == 1 and "DEPLOY BLOCKED" in r.stderr


def test_main_guard_not_executed(tmp_path):
    # Importing an entry must NOT run its __main__ block (no server start).
    _mod(tmp_path, "pkg.entry", textwrap.dedent("""
        import sys
        if __name__ == "__main__":
            sys.exit("main() ran during import!")
    """))
    r = _run(tmp_path, "pkg.entry")
    assert r.returncode == 0, r.stderr


def test_first_party_miss_is_classified_in_repo(tmp_path):
    # A tools.*-named entry importing a missing tools.* sibling classifies as
    # the in-repo (clock.py) class. Clean env + cwd=tmp so the synthetic
    # `tools` namespace never merges with the real repo's.
    _mod(tmp_path, "tools.faux.entry", "from . import gone\n")
    r = _run(tmp_path, "tools.faux.entry", cwd=str(tmp_path), clean_env=True)
    assert r.returncode == 1
    assert "in-repo module" in r.stderr
    assert "tools.faux" in r.stderr


def test_real_registry_entries_resolve():
    # No false positive on the real repo: the actual registry + DNS entry
    # points import-resolve (fastapi/uvicorn/etc. are present in this env).
    repo_root = Path(__file__).resolve().parents[4]
    r = _run(repo_root)  # default entries
    assert r.returncode == 0, f"stdout={r.stdout} stderr={r.stderr}"
