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


def test_vault_uses_shared_control_not_legacy_debug_readout():
    source = APPROVAL_RENDERER.read_text()
    assert "await openVaultApproval(self, r)" in source
    assert "'Setting: ' + setting.set_id" not in source
    adapter = (APPROVAL_RENDERER.parent.parent / 'components/vault-approval.js').read_text()
    assert "openApprovalDialog({" in adapter
    assert "name:setting.key" in adapter
    assert "requestingSession(requester.session" in adapter
    assert "['Delivered file available for',duration]" in adapter
