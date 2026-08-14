"""Fast Node contract for the production browser WebRTC adapter."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


HARNESS = Path(__file__).with_name("autonet_webrtc_harness.js")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")


def test_browser_webrtc_transport_contract():
    completed = subprocess.run(
        ["node", str(HARNESS)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "PASS: browser WebRTC transport contract" in completed.stdout
