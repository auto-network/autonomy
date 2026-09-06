"""A failed vault wake is visible: console, client-error log, shell notice.

Wraps the node suite for ceremony/vault-unlock.js's outcome reporting
(auto-uhdxm — cut from the 2026-09-06 incident where every wake failure was
silent and a dead vault hid behind a completed sign-in). The server-side
half (every pre-seat 400 logs its gate) lives in test_unlock_vault_keys.py.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest


NODE_TEST = (
    Path(__file__).resolve().parents[1]
    / "static/js/ceremony/tests/wake-vault-visibility.test.mjs"
)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not on PATH")
def test_wake_failures_surface_in_all_three_channels():
    result = subprocess.run(
        ["node", "--test", str(NODE_TEST)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
