"""Wrapper for signon_phases_submit.test.mjs — submitSignon must resolve,
not throw, when a post-cookie handoff step fails (vault-keys 500, fleet 400)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
NODE_TEST = Path(__file__).with_name("signon_phases_submit.test.mjs")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_submit_signon_reports_handoff_failures_instead_of_throwing():
    result = subprocess.run(
        ["node", "--test", str(NODE_TEST)], cwd=REPO_ROOT,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-4000:]
