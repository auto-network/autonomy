"""Node acceptance for the shared origin-neutral RelayKit browser core."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
NODE_TEST = (
    REPO_ROOT / "tools" / "dashboard" / "static" / "js"
    / "lib" / "tests" / "relaykit-core.test.mjs"
)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_shared_browser_relaykit_core():
    subprocess.run(["node", str(NODE_TEST)], cwd=REPO_ROOT, check=True)
