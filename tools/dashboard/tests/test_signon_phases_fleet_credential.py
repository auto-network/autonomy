"""Wrapper for signon_phases_fleet_credential.test.mjs — an unregistered
personal org must yield a sync-only fleet credential at sign-in."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
NODE_TEST = Path(__file__).with_name("signon_phases_fleet_credential.test.mjs")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_unregistered_personal_org_mints_sync_only_credential():
    result = subprocess.run(
        ["node", "--test", str(NODE_TEST)], cwd=REPO_ROOT,
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-4000:]
