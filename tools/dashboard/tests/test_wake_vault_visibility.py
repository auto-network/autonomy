"""A failed unlock step is visible: console, client-error log, shell notice.

Wraps the node suites for the shared surfacing mechanism
(ceremony/step-report.js) and the vault wake that reports through it
(auto-uhdxm — cut from the 2026-09-06 incident where every wake failure was
silent and a dead vault hid behind a completed sign-in; generalized for the
root-step runner's per-org steps, auto-ujrh7). The server-side half (every
pre-seat 400 logs its gate) lives in test_unlock_vault_keys.py.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest


CEREMONY_TESTS = Path(__file__).resolve().parents[1] / "static/js/ceremony/tests"
NODE_TESTS = (
    CEREMONY_TESTS / "wake-vault-visibility.test.mjs",
    CEREMONY_TESTS / "step-report.test.mjs",
)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not on PATH")
@pytest.mark.parametrize("node_test", NODE_TESTS, ids=lambda p: p.stem)
def test_step_failures_surface_in_all_three_channels(node_test):
    result = subprocess.run(
        ["node", "--test", str(node_test)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
