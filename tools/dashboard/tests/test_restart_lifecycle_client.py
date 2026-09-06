"""Client restart lifecycle regression gate using the real events.js."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


HARNESS = Path(__file__).parent / "client" / "harness_restart_lifecycle.js"
REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.skipif(shutil.which("node") is None, reason="node binary unavailable")
def test_restart_signals_converge_without_banner_resurrection():
    result = subprocess.run(
        ["node", str(HARNESS)],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "REPO_ROOT": str(REPO_ROOT)},
    )
    assert result.returncode == 0, (
        f"restart lifecycle harness failed:\n{result.stdout}\n{result.stderr}"
    )

