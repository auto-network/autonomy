"""Node acceptance wrapper for ceremony/storage.js."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
NODE_TEST = (
    REPO_ROOT / "tools" / "dashboard" / "static" / "js"
    / "ceremony" / "tests" / "storage.test.mjs"
)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_ceremony_storage_adapters(tmp_path):
    session_file = tmp_path / "ceremony-session.json"
    subprocess.run(
        ["node", str(NODE_TEST), str(session_file)],
        cwd=REPO_ROOT,
        check=True,
    )
