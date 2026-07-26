"""Cross-language acceptance wrapper for ceremony/primitives.js."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
CEREMONY_TESTS = (
    REPO_ROOT / "tools" / "dashboard" / "static" / "js"
    / "ceremony" / "tests"
)
VECTOR_GENERATOR = CEREMONY_TESTS / "generate_primitives_vectors.py"
NODE_TEST = CEREMONY_TESTS / "primitives.test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_ceremony_primitives_match_python_idkit(tmp_path):
    fixture = tmp_path / "primitives-vectors.json"
    subprocess.run(
        [sys.executable, str(VECTOR_GENERATOR), str(fixture)],
        cwd=REPO_ROOT,
        check=True,
    )
    subprocess.run(
        ["node", str(NODE_TEST), str(fixture)],
        cwd=REPO_ROOT,
        check=True,
    )
