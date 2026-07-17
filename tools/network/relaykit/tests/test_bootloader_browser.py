"""B3 headless-browser acceptance — opt-in wrapper around bootloader_harness.

The harness drives the full stack through ``agent-browser``. Because
agent-browser is flaky under the default parallel (`-n 8`) sweep — the
same reason the dashboard L2 browser tests keep a separate path — this
runs ONLY when explicitly opted in:

    AUTONET_BROWSER_TEST=1 pytest tools/network/relaykit/tests/test_bootloader_browser.py

Otherwise it skips, so the sweep stays green while the acceptance path
stays committed and one command away.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HARNESS = Path(__file__).resolve().parent / "bootloader_harness.py"
REPO = Path(__file__).resolve().parents[4]


@pytest.mark.skipif(
    os.environ.get("AUTONET_BROWSER_TEST") != "1",
    reason="set AUTONET_BROWSER_TEST=1 to run the agent-browser acceptance",
)
@pytest.mark.skipif(shutil.which("agent-browser") is None, reason="agent-browser not on PATH")
def test_bootloader_renders_binder_headless():
    result = subprocess.run(
        [sys.executable, "-m", "tools.network.relaykit.tests.bootloader_harness"],
        cwd=str(REPO), env={**os.environ, "PYTHONPATH": str(REPO)},
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
