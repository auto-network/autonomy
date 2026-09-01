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
from tools.network.idkit.root_factor_policy import mint_password_armor

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from tools.network.idkit import DelegationCert, KeyPair, derive_persona, verify_chain

HARNESS = Path(__file__).resolve().parent / "serve_cert_mint_harness.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
@pytest.mark.parametrize("mode", ["explicit", "repair"])
def test_browser_serve_cert_mint_is_idkit_compatible(mode):
    root = KeyPair.generate()
    personal = KeyPair.generate()
    genesis_id = "a1" * 32
    org_uuid = str(uuid.uuid4())
    pw = "correct horse battery staple"
    armor = mint_password_armor(root, pw, iterations=10_000)  # low iters: fast test
    personal_armor = mint_password_armor(
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
    dns01_cert = DelegationCert.from_json(posted["dns01_cert"])
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
    verify_chain(dns01_cert, root.public_hex, org=org_uuid, now=now,
                 required_scope="serve:dns-01")
    assert viewer_cert.child_pub == cert.child_pub
    assert viewer_cert.org == cert.org
    assert viewer_cert.scope == cert.scope
    assert viewer_cert.not_before == cert.not_before
    assert viewer_cert.not_after == cert.not_after
    assert viewer_cert.subject.kind == "operator"
    assert viewer_cert.subject.id == viewer_cert.child_pub
    assert cert.subject.id not in posted["viewer_cert"]
    assert dns01_cert.child_pub == cert.child_pub
    assert dns01_cert.subject == cert.subject
    assert dns01_cert.not_before == cert.not_before
    assert dns01_cert.not_after == cert.not_after
    # The exported private key is the one the cert delegates to.
    assert KeyPair.from_private_hex(posted["private_key"]).public_hex == cert.child_pub
    # A ~30-day delegate window (the operator's decision).
    span = cert.not_after - cert.not_before
    assert 29 * 86400 < span <= 30 * 86400 + 120


PERSONAL_HARNESS = (
    Path(__file__).resolve().parent / "personal_tunnel_provision_harness.js"
)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_browser_personal_tunnel_provision_is_idkit_compatible():
    """The REAL provisionPersonalNetworkIdentity brings the personal identity
    online as its OWN org (the "personal tunnel"): it self-signs the registration
    envelope AND mints the serving delegate, both with the personal root — no
    org-key, no org-scoped persona. This runs it in Node and verifies both
    artifacts with idkit."""
    from tools.network import fleet_runtime
    from tools.network.idkit import verify_signature
    from tools.network.registry.signing import request_signing_input

    personal = KeyPair.generate()
    org_uuid = fleet_runtime.personal_org_uuid(personal.public_hex)

    result = subprocess.run(
        ["node", str(PERSONAL_HARNESS)],
        env={**os.environ,
             "AUTONOMY_SEED_HEX": personal.private_hex,
             "AUTONOMY_ROOT_PUB": personal.public_hex,
             "AUTONOMY_ORG_UUID": org_uuid},
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    out = json.loads(result.stdout)

    # 1. Registration envelope — self-signed by the personal root over the
    #    registry path, binding the personal org to the personal root itself.
    assert out["register"]["org"] is None            # personal scope (org=None)
    env = out["register"]["envelope"]
    assert env["signer"] == personal.public_hex
    assert env["payload"] == {
        "org_uuid": org_uuid,
        "root_pub": personal.public_hex,
        "recovery_policy": "none",
    }
    verify_signature(
        personal.public_hex, env["sig"],
        request_signing_input("POST", "/v1/orgs", env["ts"],
                              env["signer"], env["payload"]),
    )

    # 2. Serving delegate — chains to the personal root, persona subject is the
    #    personal root, viewer cert identity-neutral, key matches child_pub.
    assert out["serve"]["org"] is None
    cert = DelegationCert.from_json(out["serve"]["cert"])
    viewer_cert = DelegationCert.from_json(out["serve"]["viewer_cert"])
    dns01_cert = DelegationCert.from_json(out["serve"]["dns01_cert"])
    now = (cert.not_before + cert.not_after) // 2
    verify_chain(cert, personal.public_hex, org=org_uuid, now=now,
                 required_scope="tunnel:serve")
    verify_chain(viewer_cert, personal.public_hex, org=org_uuid, now=now,
                 required_scope="tunnel:serve")
    verify_chain(dns01_cert, personal.public_hex, org=org_uuid, now=now,
                 required_scope="serve:dns-01")
    assert cert.subject.kind == "persona"
    assert cert.subject.id == personal.public_hex
    assert viewer_cert.subject.kind == "operator"
    assert viewer_cert.subject.id == viewer_cert.child_pub
    assert KeyPair.from_private_hex(out["serve"]["private_key"]).public_hex == \
        cert.child_pub


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_sign_on_checks_serving_but_cannot_mint_from_a_legacy_org_armor():
    """Sign-on renews serving credentials, because the personal seed it holds
    opens each organization's SEALED root without a second passphrase.

    A legacy passphrase-armored organization key is the one case it cannot
    open: that armor answers to the ORGANIZATION's own passphrase, which a
    personal unlock does not have and must not prompt for. So the status is
    checked, the key is read, nothing is minted, and the organization is
    reported as ``legacy-org-armor`` rather than passing silently."""
    root = KeyPair.generate()
    personal = KeyPair.generate()
    pw = "correct horse battery staple"
    result = subprocess.run(
        ["node", str(HARNESS)],
        env={
            **os.environ,
            "AUTONOMY_ARMOR": mint_password_armor(root, pw, iterations=10_000),
            "AUTONOMY_PERSONAL_ARMOR": mint_password_armor(
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
        "captured": None, "org_key_reads": 1, "serve_status_reads": 1,
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
            "AUTONOMY_ARMOR": mint_password_armor(
                root, pw, iterations=10_000),
            "AUTONOMY_PERSONAL_ARMOR": mint_password_armor(
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
