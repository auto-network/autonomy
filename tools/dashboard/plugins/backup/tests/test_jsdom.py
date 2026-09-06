"""jsdom acceptance for the /backup fragment.

Same convention as the mission plugin: a self-contained .cjs harness
(node + jsdom + vendored Alpine) driven from here; skips cleanly when
node or jsdom is unavailable.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_HARNESS = Path(__file__).parent / "jsdom" / "backup_page.cjs"


def test_backup_page_fragment_sweep():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    proc = subprocess.run(
        [node, str(_HARNESS)], capture_output=True, text=True, timeout=120)
    combined = (proc.stdout or "") + (proc.stderr or "")
    if "MODULE_NOT_FOUND" in combined or "Cannot find module 'jsdom'" in combined:
        pytest.skip("jsdom not resolvable")
    assert proc.returncode == 0, f"backup_page failed:\n{combined}"
    assert "PASS backup_page" in proc.stdout, combined
