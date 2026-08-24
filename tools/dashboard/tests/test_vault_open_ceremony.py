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


def test_existing_approval_sheet_names_requester_and_full_setting():
    source = APPROVAL_RENDERER.read_text()
    assert "'Setting: ' + setting.set_id + ' / ' + setting.key" in source
    assert "'Requesting organization: ' + requester.organization" in source
    assert "'Requesting session: ' + requester.session" in source
    assert "'Workspace: ' + requester.workspace" in source
    assert "'TTL: ' + req.ttl_seconds" in source
