"""Toolchain smoke test for the browser-free (jsdom) UI test path.

The dashboard's Category-B UI tests — data -> render -> interact -> assert DOM,
no real browser, no layout engine — run the vendored Alpine build inside jsdom
(a DOM-in-Node implementation). This test proves that toolchain end to end:
node can load jsdom + Alpine and drive reactive rendering.

It SKIPS when node or jsdom is unavailable (e.g. before the agent image that
installs jsdom globally is built — see agents/Dockerfile / NODE_PATH), so it is
safe in every environment and turns green the moment the toolchain is present.

Layout/positioning tests (getBoundingClientRect) are deliberately NOT in this
path: jsdom has no layout engine, so those stay on a real browser.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

_SMOKE = Path(__file__).resolve().parents[1] / "test_lib" / "jsdom" / "alpine_smoke.cjs"


def _node() -> str | None:
    return shutil.which("node")


def test_jsdom_alpine_toolchain():
    node = _node()
    if node is None:
        pytest.skip("node not available")
    if not _SMOKE.exists():
        pytest.skip(f"smoke script missing: {_SMOKE}")

    proc = subprocess.run(
        [node, str(_SMOKE)],
        capture_output=True, text=True, timeout=60,
    )
    combined = (proc.stdout or "") + (proc.stderr or "")
    # CommonJS require -> "Cannot find module 'jsdom'" / code MODULE_NOT_FOUND;
    # ESM import -> ERR_MODULE_NOT_FOUND. Cover both so a missing jsdom skips
    # (toolchain absent) rather than failing.
    if "MODULE_NOT_FOUND" in combined or "Cannot find module 'jsdom'" in combined:
        pytest.skip(
            "jsdom not resolvable (install it globally + set "
            "NODE_PATH=/usr/lib/node_modules — see agents/Dockerfile)"
        )
    assert proc.returncode == 0, f"jsdom smoke failed:\n{combined}"
    assert "PASS" in proc.stdout, f"unexpected smoke output:\n{combined}"
