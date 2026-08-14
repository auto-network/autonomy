"""Node acceptance wrapper for the org:join channel transport layer.

Drives ceremony/claim.js (unchanged) over a stub SecureChannel through the
provisional sendOp shim + channel-backed transport.fetch adapter, asserting
the op reframing, that invite_ref never rides the wire, brand pass-through,
and the ledger-truth / link-truth split.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
NODE_TEST = (
    REPO_ROOT / "tools" / "dashboard" / "static" / "js"
    / "join" / "tests" / "channel-transport.test.mjs"
)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_join_channel_transport():
    subprocess.run(
        ["node", str(NODE_TEST)],
        cwd=REPO_ROOT,
        check=True,
    )
