"""org:join publish stays coherent when genesis UUID != registry-binding UUID.

Regression for auto-hzs4f: publishing an org:join link for a legacy org (whose
ledger-genesis UUID differs from its registered/binding UUID — e.g. autonomy)
failed at execute with "signed payload does not match the staged request",
because the staged registry payload carried target_uuid = genesis while the
browser signer (buildOrgJoinGrantPayload) and the registry law both require
target_uuid = org = binding.

The fix (ruling b): the org:join staging reconciles to the binding UUID, so the
published identity is always the binding. These tests assert that reconciliation
AND drive the REAL browser signer against the REAL staged payload with
genesis != binding: the browser rebuilds its signed payload from the staged
request (worktrees.js), so any rebuild divergence would make the operator sign
something other than what the dialog showed. (Execution itself now rides the
org tunnel — auto-qol1v — and the executor independently reconciles
target_uuid to the binding; this pins the render/sign coherence.) The earlier
Python harness signed the staged payload verbatim, which is why the browser's
rebuild divergence shipped untested; this closes that gap.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tools.dashboard import link_approvals


REPO_ROOT = Path(__file__).resolve().parents[3]
NODE_DRIVER = (
    REPO_ROOT
    / "tools/dashboard/static/js/ceremony/tests/org-join-grant-vector.mjs"
)
GENESIS = "11111111-1111-4111-8111-111111111111"
BINDING = "2d4b90cb-1e89-452b-82cb-68ca44fd8e52"  # autonomy's real split
INVITE_REF = "ab" * 32
EXPIRY = 1_900_000_000_000


def _binding():
    return {
        "org_uuid": BINDING,
        "root_pub": "cd" * 32,
        "registry_url": "http://registry.test",
    }


def _org_join_req(meta=None):
    # The client sends target_uuid = genesis (the founded-org identity the
    # create-gate validates against the ledger).
    req = {
        "org": "autonomy",
        "target_uuid": GENESIS,
        "target_type": "org:join",
        "invite_ref": INVITE_REF,
        "expires_at": EXPIRY,
    }
    if meta is not None:
        req["meta"] = meta
    return req


def test_org_join_staging_reconciles_target_uuid_to_the_binding():
    staged = link_approvals._registry_payload(_org_join_req(), _binding())
    # The published identity is the binding, and org == target_uuid (registry
    # law + browser signer). This is the reconciliation that was missing.
    assert staged["org"] == BINDING
    assert staged["target_uuid"] == BINDING
    assert staged["invite_ref"] == INVITE_REF
    assert staged["expires_at"] == EXPIRY


def test_non_org_join_share_keeps_its_asset_uuid():
    """A note/mission/etc. share still names its asset, not the org binding."""
    asset = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    req = {"org": "autonomy", "target_uuid": asset, "target_type": "present"}
    staged = link_approvals._registry_payload(req, _binding())
    assert staged["org"] == BINDING
    assert staged["target_uuid"] == asset


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
@pytest.mark.parametrize("meta", [None, {"label": "Join autonomy"}])
def test_browser_signer_matches_staging_under_uuid_split(meta):
    staged = link_approvals._registry_payload(_org_join_req(meta), _binding())
    result = subprocess.run(
        ["node", str(NODE_DRIVER), json.dumps(staged)],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    signed = json.loads(result.stdout)
    # What the browser signs must equal what the dialog staged (render/sign
    # coherence). Genesis != binding here, yet they match — the divergence
    # that live-failed is pinned closed.
    assert signed == staged
