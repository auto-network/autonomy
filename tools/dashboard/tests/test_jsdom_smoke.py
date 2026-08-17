"""Browser-free (jsdom) dashboard UI tests.

Each ``*.cjs`` under ``tools/dashboard/test_lib/jsdom/`` is a self-contained
node script that runs the dashboard's client JS + vendored Alpine inside jsdom
(DOM-in-Node) and asserts on the result — data -> render -> interact -> DOM, with
no real browser and no layout engine. A script exits 0 on success, non-zero on
failure. See test_lib/jsdom/README.md for scope (Category B here; layout stays
on a real browser) and the migration recipe.

Tests SKIP when node or jsdom is unavailable (e.g. before the agent image that
installs jsdom globally is built — see agents/Dockerfile / NODE_PATH), so they
are safe everywhere and turn green once the toolchain is present.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

_JSDOM_DIR = Path(__file__).resolve().parents[1] / "test_lib" / "jsdom"
_SCRIPTS = sorted(_JSDOM_DIR.glob("*.cjs"))


@pytest.mark.parametrize("script", _SCRIPTS, ids=[s.stem for s in _SCRIPTS])
def test_jsdom_script(script: Path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")

    proc = subprocess.run(
        [node, str(script)], capture_output=True, text=True, timeout=60,
    )
    combined = (proc.stdout or "") + (proc.stderr or "")
    # require -> "Cannot find module 'jsdom'" / MODULE_NOT_FOUND; ESM -> ERR_MODULE_NOT_FOUND.
    if "MODULE_NOT_FOUND" in combined or "Cannot find module 'jsdom'" in combined:
        pytest.skip(
            "jsdom not resolvable (install it globally + set "
            "NODE_PATH=/usr/lib/node_modules — see agents/Dockerfile)"
        )
    assert proc.returncode == 0, f"{script.name} failed:\n{combined}"
    assert "PASS" in proc.stdout, f"{script.name} unexpected output:\n{combined}"
