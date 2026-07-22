"""Cross-language proof: the browser serve-cert mint is idkit-compatible.

``network-signon.js`` provisionServeCert mints the serving delegate in
WebCrypto during a publish approve. This runs that REAL function in Node
against a Python-generated org armor, captures the ``{cert, private_key}`` it
POSTs, and verifies with the REAL idkit that the cert chains to the org root
with ``tunnel:serve`` scope and that the exported key matches the cert — i.e.
what the browser produces is exactly what the registry, the handshake, and the
provision gate will accept. If the JS cert construction ever drifts from idkit,
this fails.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from tools.network.idkit import DelegationCert, KeyPair, verify_chain
from tools.network.idkit.armor import encrypt_root_key

HARNESS = Path(__file__).resolve().parent / "serve_cert_mint_harness.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_browser_serve_cert_mint_is_idkit_compatible():
    root = KeyPair.generate()
    org_uuid = str(uuid.uuid4())
    pw = "correct horse battery staple"
    armor = encrypt_root_key(root, pw, iterations=10_000)  # low iters: fast test

    result = subprocess.run(
        ["node", str(HARNESS)],
        env={**os.environ, "AUTONOMY_ARMOR": armor, "AUTONOMY_PW": pw,
             "AUTONOMY_ORG_UUID": org_uuid},
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    posted = json.loads(result.stdout)

    cert = DelegationCert.from_json(posted["cert"])
    # Exactly what the handshake / provision gate re-check: chains to the org
    # root, tunnel:serve scope, right org.
    now = (cert.not_before + cert.not_after) // 2
    verify_chain(cert, root.public_hex, org=org_uuid, now=now,
                 required_scope="tunnel:serve")
    assert tuple(cert.scope) == ("tunnel:serve",)
    assert cert.org == org_uuid
    assert cert.subject.kind == "operator"
    # The exported private key is the one the cert delegates to.
    assert KeyPair.from_private_hex(posted["private_key"]).public_hex == cert.child_pub
    # A ~30-day delegate window (the operator's decision).
    span = cert.not_after - cert.not_before
    assert 29 * 86400 < span <= 30 * 86400 + 120
