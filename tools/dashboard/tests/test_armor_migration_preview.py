"""The v2→v3 migration payload, REAL client builder vs REAL server validation.

Reproduces the 2026-08-27 production failure: the operator's live v2 armor
carried a passkey wrap whose credential has NO dashboard registration row (an
orphan — unusable for sign-in). The one-shot login migration faithfully
carried it into the v3 factor list, and the server's passkey-binding check
refused the whole batch (preview 400), so the armor stayed v2.

Cross-implementation on purpose: the operations come from the REAL JS builder
(ceremony/armor-migration.js, the code unlock.js runs), and are validated by
the REAL Python projection (_project_factor_policy) plus the REAL binding
check (_validate_passkey_bindings with the registration rows monkeypatched to
the scenario). No mirrors on either side — the earlier suites missed this
precisely because their in-process server stub implements neither
migrate_legacy nor the binding check.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not on PATH"
)

_HERE = os.path.dirname(os.path.abspath(__file__))
_CEREMONY = os.path.join(
    os.path.dirname(_HERE), "static", "js", "ceremony",
)

ROOT_PUB = "a" * 64
REGISTERED_CRED = "Gi8Aj8EbUBDOCMutbYIXqsEyyzc"
ORPHAN_CRED = "lF9Sg9cBn1gbrvNcNAT3blEo95c"


def _live_style_view(passkey_creds):
    """A legacy factor-policy view shaped like the live one."""
    factors = [{
        "factor_id": "legacy.password", "type": "password",
        "kdf": {"name": None, "hash": None, "iterations": None},
    }]
    for cred in passkey_creds:
        suffix = hashlib.sha256(cred.encode()).hexdigest()[:20]
        factors.append({
            "factor_id": f"legacy.passkey:{suffix}", "type": "passkey",
            "credential_id": cred,
            "recipients": [{
                "recipient_public_key": "b" * 64,
                "label": "Legacy device",
                "created_at": "1970-01-01T00:00:00Z",
            }],
        })
    return {"root_pub": ROOT_PUB, "factors": factors}


def _build_operations(view, registered_creds):
    """Run the REAL JS migration builder exactly as unlock.js does."""
    script = f"""
import {{ buildMigrationOperations }} from 'file://{_CEREMONY}/armor-migration.js';
const view = {json.dumps(view)};
const registered = {json.dumps(sorted(registered_creds))};
const ops = await buildMigrationOperations(view, {{
  password: 'migration-test-password',
  registeredCredentialIds: registered,
}});
console.log(JSON.stringify(ops));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def _preview(operations, registered_creds, monkeypatch):
    """Run the REAL server-side validation the preview endpoint performs."""
    from tools.dashboard import identity_routes

    class _Row:
        def __init__(self, cred):
            self.payload = {"credential_id": cred}

    monkeypatch.setattr(
        identity_routes, "_passkey_rows",
        lambda: [_Row(cred) for cred in registered_creds],
    )
    state = {
        "migration_required": True,
        "generation": 0,
        "root_pub": ROOT_PUB,
        "factors": [], "access": [], "root_policy": None,
    }
    projected = identity_routes._project_factor_policy(state, operations)
    identity_routes._validate_passkey_bindings(projected)
    return projected


def test_migration_accepted_when_every_armor_passkey_is_registered(monkeypatch):
    registered = [REGISTERED_CRED, ORPHAN_CRED]
    view = _live_style_view([REGISTERED_CRED, ORPHAN_CRED])
    ops = _build_operations(view, registered)
    projected = _preview(ops, registered, monkeypatch)
    assert projected["generation"] == 1
    assert projected["roles"]["legacy.password"]["root_role"] == "individual"


def test_migration_survives_an_orphaned_armor_passkey(monkeypatch):
    """The production case: one armor passkey has no registration row.

    The migration must still succeed — the orphan cannot sign in today, so it
    is dead weight the migration drops rather than a factor to preserve.
    """
    registered = [REGISTERED_CRED]
    view = _live_style_view([REGISTERED_CRED, ORPHAN_CRED])
    ops = _build_operations(view, registered)
    projected = _preview(ops, registered, monkeypatch)
    assert projected["generation"] == 1
    carried = {f["credential_id"] for f in projected["factors"]
               if f["type"] == "passkey"}
    assert REGISTERED_CRED in carried, "the registered passkey carries over"
    assert ORPHAN_CRED not in carried, "the orphan is dropped, not fatal"
