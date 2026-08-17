"""Cross-language proof: the browser serve-cert mint is idkit-compatible.

``network-signon.mjs`` provisionServeCert mints the serving delegate in
WebCrypto when a password-backed dashboard unlock finds that repair is
required. This runs that REAL function in Node against a Python-generated org
armor, captures the two certificates and their shared private key, and
verifies both with the real idkit. The registry cert must carry the persona;
the viewer cert must carry no persona-derived value.

Minting a serving delegate is a ROOT-DIRECT constitutional act (design §8) —
the org root ceremony is the only thing that binds a persona to a serving
child. Sign-on does not reach it, and must not: sign-on opens the personal
root only, and never an organization's key.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from tools.network.idkit import DelegationCert, KeyPair, derive_persona, verify_chain
from tools.network.idkit.armor import encrypt_root_key

HARNESS = Path(__file__).resolve().parent / "serve_cert_mint_harness.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
@pytest.mark.parametrize("mode", ["explicit", "repair"])
def test_browser_serve_cert_mint_is_idkit_compatible(mode):
    root = KeyPair.generate()
    personal = KeyPair.generate()
    genesis_id = "a1" * 32
    org_uuid = str(uuid.uuid4())
    pw = "correct horse battery staple"
    armor = encrypt_root_key(root, pw, iterations=10_000)  # low iters: fast test
    personal_armor = encrypt_root_key(
        personal, pw, iterations=10_000)  # low iters: fast test

    result = subprocess.run(
        ["node", str(HARNESS)],
        env={**os.environ, "AUTONOMY_ARMOR": armor,
             "AUTONOMY_PERSONAL_ARMOR": personal_armor,
             "AUTONOMY_GENESIS_ID": genesis_id, "AUTONOMY_PW": pw,
             "AUTONOMY_ORG_UUID": org_uuid,
             "AUTONOMY_ROOT_PUB": root.public_hex,
             "AUTONOMY_MODE": mode},
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    posted = json.loads(result.stdout)

    cert = DelegationCert.from_json(posted["cert"])
    viewer_cert = DelegationCert.from_json(posted["viewer_cert"])
    # Exactly what the handshake / provision gate re-check: chains to the org
    # root, tunnel:serve scope, right org.
    now = (cert.not_before + cert.not_after) // 2
    verify_chain(cert, root.public_hex, org=org_uuid, now=now,
                 required_scope="tunnel:serve")
    assert tuple(cert.scope) == ("tunnel:serve",)
    assert cert.org == org_uuid
    assert cert.subject.kind == "persona"
    assert cert.subject.id == derive_persona(
        bytes.fromhex(personal.private_hex), genesis_id).public_hex
    verify_chain(viewer_cert, root.public_hex, org=org_uuid, now=now,
                 required_scope="tunnel:serve")
    assert viewer_cert.child_pub == cert.child_pub
    assert viewer_cert.org == cert.org
    assert viewer_cert.scope == cert.scope
    assert viewer_cert.not_before == cert.not_before
    assert viewer_cert.not_after == cert.not_after
    assert viewer_cert.subject.kind == "operator"
    assert viewer_cert.subject.id == viewer_cert.child_pub
    assert cert.subject.id not in posted["viewer_cert"]
    # The exported private key is the one the cert delegates to.
    assert KeyPair.from_private_hex(posted["private_key"]).public_hex == cert.child_pub
    # A ~30-day delegate window (the operator's decision).
    span = cert.not_after - cert.not_before
    assert 29 * 86400 < span <= 30 * 86400 + 120


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_sign_on_neither_reads_the_org_key_nor_mints_a_serving_delegate():
    """Sign-on opens the personal root and nothing else. Serving repair is a
    root ceremony reached from the password-backed unlock hook, not from the
    personal unlock, so a sign-on leaves both alone even when the stored
    serving credential is reported missing."""
    root = KeyPair.generate()
    personal = KeyPair.generate()
    pw = "correct horse battery staple"
    result = subprocess.run(
        ["node", str(HARNESS)],
        env={
            **os.environ,
            "AUTONOMY_ARMOR": encrypt_root_key(root, pw, iterations=10_000),
            "AUTONOMY_PERSONAL_ARMOR": encrypt_root_key(
                personal, pw, iterations=10_000),
            "AUTONOMY_GENESIS_ID": "a1" * 32,
            "AUTONOMY_PW": pw,
            "AUTONOMY_ORG_UUID": str(uuid.uuid4()),
            "AUTONOMY_ROOT_PUB": root.public_hex,
            "AUTONOMY_MODE": "signon",
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    assert json.loads(result.stdout) == {
        "captured": None, "org_key_reads": 0, "serve_status_reads": 0,
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_dashboard_unlock_repair_check_does_not_mint_when_credential_is_ready():
    root = KeyPair.generate()
    personal = KeyPair.generate()
    pw = "correct horse battery staple"
    result = subprocess.run(
        ["node", str(HARNESS)],
        env={
            **os.environ,
            "AUTONOMY_ARMOR": encrypt_root_key(
                root, pw, iterations=10_000),
            "AUTONOMY_PERSONAL_ARMOR": encrypt_root_key(
                personal, pw, iterations=10_000),
            "AUTONOMY_GENESIS_ID": "a1" * 32,
            "AUTONOMY_PW": pw,
            "AUTONOMY_ORG_UUID": str(uuid.uuid4()),
            "AUTONOMY_ROOT_PUB": root.public_hex,
            "AUTONOMY_MODE": "repair-ready",
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    proof = json.loads(result.stdout)
    assert proof == {"captured": None, "serve_status_reads": 1}
