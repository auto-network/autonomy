"""Browser-orchestration contract for serving repair after password unlock."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


HARNESS = Path(__file__).resolve().parent / "unlock_serve_repair_harness.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
@pytest.mark.parametrize("mode", ["success", "failure"])
def test_password_unlock_runs_opportunistic_serving_repair_after_access(mode):
    result = subprocess.run(
        ["node", str(HARNESS)],
        env={**os.environ, "AUTONOMY_REPAIR_MODE": mode},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    proof = json.loads(result.stdout)
    assert proof["repair_called_after_access"] is True
    assert proof["repair_calls"] == 1
    assert proof["events"][-1] == "POST /api/identity/unlock/password"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_access_only_passkey_unlock_does_not_attempt_root_signed_repair():
    result = subprocess.run(
        ["node", str(HARNESS)],
        env={**os.environ, "AUTONOMY_REPAIR_MODE": "passkey"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    proof = json.loads(result.stdout)
    assert proof["repair_calls"] == 0
    assert proof["events"][-1] == "POST /api/identity/unlock/passkey"
