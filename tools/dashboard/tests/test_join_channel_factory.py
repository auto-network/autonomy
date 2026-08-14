"""Node acceptance wrapper for the org:join channel factory.

Proves the wsUrl derivation (from channelToken against the fixed relay origin,
never the page origin) and the relaykit/core handshake call shape, using an
injected core so it runs before relaykit-core.js is served own-origin.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
NODE_TEST = (
    REPO_ROOT / "tools" / "dashboard" / "static" / "js"
    / "join" / "tests" / "channel-factory.test.mjs"
)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_join_channel_factory():
    subprocess.run(
        ["node", str(NODE_TEST)],
        cwd=REPO_ROOT,
        check=True,
    )
