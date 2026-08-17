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
def test_the_password_unlock_migrates_legacy_org_keys_before_repairing():
    """An organization whose root key is still passphrase-armored cannot be
    opened from a personal unlock, so its serving credential can NEVER be
    repaired. The migration therefore has to run first, and it has to run
    here -- this is the function a person reaches by signing in, and the only
    one holding the password.
    """
    proof = _run()
    assert proof["migrate_calls"] == 1
    assert proof["migrate_before_repair"] is True, \
        "repairing before migrating leaves the legacy org unreachable"
    assert proof["repair_calls"] == 1
    assert proof["migration"] == {"migrated": ["alpha-org"], "notMigrated": []}


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_a_failed_migration_does_not_cost_the_operator_their_session():
    """Access has already been granted by the time this runs. A migration that
    fails must not undo it, and must not stop the repair that follows."""
    proof = _run(mode="migrate-failure")
    assert "POST /api/identity/unlock/password" in proof["events"]
    assert proof["repair_calls"] == 1, "the repair still runs"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_the_passkey_path_does_not_migrate():
    """A passkey unlock releases no signing material, so it can open no org
    root -- it must not even attempt this."""
    proof = _run(mode="passkey")
    assert proof["migrate_calls"] == 0
    assert proof["repair_calls"] == 0
