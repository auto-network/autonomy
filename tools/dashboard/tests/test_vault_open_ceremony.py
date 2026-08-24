"""Node coverage for the browser-side vault factor gatherer."""

from __future__ import annotations

import subprocess
from pathlib import Path


NODE_TEST = (
    Path(__file__).resolve().parents[1]
    / "static/js/ceremony/tests/open-vault.test.mjs"
)


def test_vault_open_browser_ceremony():
    result = subprocess.run(
        ["node", "--test", str(NODE_TEST)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
