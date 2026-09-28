"""Node coverage for the browser-side vault factor gatherer."""

from __future__ import annotations

import subprocess
from pathlib import Path


NODE_TEST = (
    Path(__file__).resolve().parents[1]
    / "static/js/ceremony/tests/open-vault.test.mjs"
)
APPROVAL_RENDERER = (
    Path(__file__).resolve().parents[1]
    / "static/js/pages/worktrees.js"
)


def test_vault_open_browser_ceremony():
    result = subprocess.run(
        ["node", "--test", str(NODE_TEST)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_vault_open_renders_only_on_central():
    """vault_open is a Central approval (auto-fkhq0.27): the legacy worktrees
    overlay no longer opens it, and its legacy adapter is gone."""
    source = APPROVAL_RENDERER.read_text()
    assert "vault_open:" not in source
    assert "openVaultApproval" not in source
    assert not (APPROVAL_RENDERER.parent.parent / "components/vault-approval.js").exists()
