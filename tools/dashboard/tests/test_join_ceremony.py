"""Node acceptance wrapper for the org:join acceptance ceremony seam.

Proves the browser-local orchestration and its I1 guarantees against injected
fakes: the passphrase opens the armor and is never returned or sent, the root
and kem seeds are zeroed on every path (including a thrown mint), and only the
signed public claim plus the invitee's own kem key come back.
"""

from __future__ import annotations

import shutil
import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
NODE_TEST = (
    REPO_ROOT / "tools" / "dashboard" / "static" / "js"
    / "join" / "tests" / "ceremony.test.mjs"
)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_join_ceremony_seam():
    subprocess.run(
        ["node", str(NODE_TEST)],
        cwd=REPO_ROOT,
        check=True,
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
@pytest.mark.parametrize("entrypoint", ["password", "root"])
def test_join_key_rederived_at_signin_opens_grant(entrypoint):
    from tools.network.idkit import KeyPair
    from tools.network.ledger.projections import organization_content_domain_id
    from tools.network.storagekit import credentials, state, capability
    result = subprocess.run(
        ["node", str(NODE_TEST.with_name("kem-recovery.mjs")), entrypoint],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    received = json.loads(result.stdout)
    credential = credentials.validate(received["credential"])
    author = KeyPair.generate()
    domain = organization_content_domain_id(credential.genesis_id)
    descriptor, secret = state.generate(author, credential.genesis_id, domain,
                                       [], credential.authority_heads, [], "00" * 32)
    grant = capability.issue(author, genesis_id=credential.genesis_id,
        domain_id=domain, storage_state_id=descriptor.state_id,
        recipient_credential=credential, state_secret=secret,
        state_secret_commitment=descriptor.secret_commitment,
        authority_heads=credential.authority_heads)
    assert capability.accept(grant, received["private"], descriptor) == secret
