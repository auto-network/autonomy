"""Browser-orchestration contract for serving repair after password unlock."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


HARNESS = Path(__file__).resolve().parent / "unlock_serve_repair_harness.js"


def _proof(stdout: str) -> dict:
    """The harness's result is the LAST line: the code under test also writes
    an operator-facing console line, which shares stdout under node."""
    return json.loads([ln for ln in stdout.splitlines() if ln.strip()][-1])


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
    proof = _proof(result.stdout)
    assert proof["repair_called_after_access"] is True
    assert proof["repair_calls"] == 1
    # Access authentication completes BEFORE any maintenance -- the ordering
    # is the property, not that the unlock is the final call (maintenance now
    # reports its outcome to the server afterwards).
    events = proof["events"]
    assert "POST /api/identity/unlock/password" in events
    maintenance = [i for i, e in enumerate(events) if "unlock-report" in e]
    if maintenance:
        assert events.index("POST /api/identity/unlock/password") < maintenance[0]


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
    proof = _proof(result.stdout)
    assert proof["repair_calls"] == 0
    assert proof["events"][-1] == "POST /api/identity/unlock/passkey"


def _run(mode: str = "success") -> dict:
    result = subprocess.run(
        ["node", str(HARNESS)],
        env={**os.environ, "AUTONOMY_REPAIR_MODE": mode},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    return _proof(result.stdout)





@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_the_unlock_repairs_every_organization_not_just_the_default():
    """repairServeCredential covers ONE organization -- the one named, or the
    caller's default. An unlock that called it once maintained that one and
    left every other organization to expire, which is how two of three drifted
    to within days while the third stayed healthy."""
    result = subprocess.run(
        ["node", str(HARNESS)],
        env={**os.environ, "AUTONOMY_REPAIR_MODE": "success",
             "AUTONOMY_ALL_ORG_REPAIR": "1"},
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    proof = _proof(result.stdout)
    assert proof["all_repair_calls"] == 1
    assert proof["all_repair_orgs"] == ["autonomy", "dynbench", "anchore"]
    assert proof["repair_calls"] == 0, "the single-org path is not used as well"
    assert proof["serve_repair"]["repaired"] == [
        "autonomy", "dynbench", "anchore"]
