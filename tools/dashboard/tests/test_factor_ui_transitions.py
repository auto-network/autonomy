"""Run the JSDOM factor-management transition suite under pytest.

The real coverage lives in ``factor_management_transitions.test.mjs`` — it drives
the actual panel UI in JSDOM against the real re-arm crypto (node webcrypto), no
browser. This wrapper makes that suite part of the ordinary Python test run so a
UI-wiring regression (a transition that no longer reaches the backend, or reaches
it with the wrong opener) fails CI the same as any other test.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not on PATH"
)

_HERE = os.path.dirname(os.path.abspath(__file__))


@pytest.mark.parametrize("mjs", [
    "factor_management_transitions.test.mjs",
    "open_root.test.mjs",
])
def test_ui_ceremony_jsdom(mjs):
    result = subprocess.run(
        ["node", "--test", os.path.join(_HERE, mjs)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
