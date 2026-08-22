"""Runs the home-page list-behaviour node suite (bead auto-acyib).

The list logic is client code; the node suite exercises the real component
factory browser-free. This wrapper makes it part of every pytest run so a
regression in the two damage-class behaviours (default view excluding
completed missions; two-click removal) fails CI, not the operator.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_JS = Path(__file__).resolve().parent / "test_home_list.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_home_list_behaviour_node_suite():
    result = subprocess.run(
        ["node", "--test", str(_JS)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, (
        "node suite failed:\n" + result.stdout[-4000:] + result.stderr[-2000:]
    )
