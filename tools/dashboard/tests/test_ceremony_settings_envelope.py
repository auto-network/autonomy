"""Cross-language acceptance wrapper for ceremony/settings-envelope.js.

One vector set, encoded by both builders, asserted equal byte for byte —
auto-4oxee. Python (tools/network/settingskit/envelope.py) generates the
vectors; Node re-encodes, re-signs, and cross-verifies them.
"""

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
VECTOR_GENERATOR = CEREMONY_TESTS / "generate_settings_envelope_vectors.py"
NODE_TEST = CEREMONY_TESTS / "settings-envelope.test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_settings_envelope_matches_python(tmp_path):
    fixture = tmp_path / "settings-envelope-vectors.json"
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
