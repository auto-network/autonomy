"""The bootloader connect/handshake is bounded — the "that hung" regression.

A relay may accept a viewer WebSocket and never send SERVER_HELLO (it holds
the connection open when no serving tunnel is dialed in for the org). The
bootloader's ``recvBinary`` waits forever, so without a bound the viewer page
hangs on a spinner and never reaches the honest offline error. This runs the
Node harness that drives the REAL ``performHandshake`` against a silent
transport and asserts it rejects fast rather than hanging.

Deterministic and dependency-light (Node + webcrypto only), so it stays in
the default sweep — unlike the opt-in agent-browser acceptance.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).resolve().parent / "autonet_timeout_harness.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_bootloader_handshake_is_bounded():
    result = subprocess.run(
        ["node", str(HARNESS)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (
        f"bootloader handshake was not bounded:\n{result.stdout}\n{result.stderr}"
    )
    assert "PASS" in result.stdout, result.stdout
