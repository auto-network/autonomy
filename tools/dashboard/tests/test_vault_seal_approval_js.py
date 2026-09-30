"""Wrapper for vault_seal_approval.test.mjs — the Central renderer for a
``vault_seal`` request: masked secret entry gates approval, the value goes to
the deposit route only, the decision names only the sealed row, and a refused
deposit leaves the typed value in place for a retry."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
NODE_TEST = Path(__file__).with_name("vault_seal_approval.test.mjs")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_vault_seal_renderer_contract():
    result = subprocess.run(
        ["node", "--test", str(NODE_TEST)], cwd=REPO_ROOT,
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-4000:]
