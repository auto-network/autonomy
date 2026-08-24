"""The personal vault recipient is login bootstrap, never a second UI ceremony."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest


NODE_TEST = (
    Path(__file__).resolve().parents[1]
    / "static/js/ceremony/tests/vault-unlock-bootstrap.test.mjs"
)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not on PATH")
def test_vault_anchor_bootstraps_during_root_opening_login():
    result = subprocess.run(
        ["node", "--test", str(NODE_TEST)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
