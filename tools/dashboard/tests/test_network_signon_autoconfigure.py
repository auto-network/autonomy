"""Real module-load regression for browser sign-on adapter configuration."""

from __future__ import annotations
from tools.network.idkit.root_factor_policy import mint_password_armor

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from tools.network.idkit import (
    DelegationCert,
    KeyPair,
    derive_persona,
    verify_chain,
)
from tools.network.idkit.keys import verify_signature
from tools.network.registry.signing import request_signing_input

HARNESS = (
    Path(__file__).resolve().parent
    / "network_signon_autoconfigure_harness.mjs"
)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_browser_module_load_configures_real_signon_path():
    personal = KeyPair.generate()
    org_uuid = str(uuid.uuid4())
    genesis_id = "b7" * 32
    passphrase = "module load browser sign-on passphrase"
    result = subprocess.run(
        ["node", str(HARNESS), "browser"],
        env={
            **os.environ,
            "AUTONOMY_PERSONAL_ARMOR": mint_password_armor(
                personal, passphrase, iterations=10_000),
            "AUTONOMY_PERSONAL_ROOT_PUB": personal.public_hex,
            "AUTONOMY_GENESIS_ID": genesis_id,
            "AUTONOMY_ROOT_PUB": personal.public_hex,
            "AUTONOMY_ORG_UUID": org_uuid,
            "AUTONOMY_PASSPHRASE": passphrase,
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    output = json.loads(result.stdout)

    persona = derive_persona(bytes.fromhex(personal.private_hex), genesis_id)

    assert output["state"]["signedIn"] is True
    assert output["storedSessions"] == 1
    # No browser label is minted any more: the actor is the persona, so the
    # localStorage stand-in identity is never written.
    assert output["subjectId"] is None
    # The ONE unlock reads the personal armor, then one ledger head, one
    # binding, one re-key policy and one serving-certificate status per
    # organization. The serving check is READ-ONLY and needs no key; the
    # organization key itself is fetched only when a certificate is actually
    # due for renewal, which it is not here.
    #
    # The trailing checkpoint probe is `signOn`'s — the worktrees-approval
    # path, which still hand-wires its per-org maintenance instead of running
    # the plan-gated step registry the dashboard unlock uses
    # (repairAllServeCredentialsWithRootSeed). On that path the same probe is
    # not made at all unless a checkpoint is genuinely due AND this persona may
    # publish it. Converting signOn to the one runner is auto-77y5a's
    # remaining piece; until then this call is real and asserted, not wished
    # away.
    assert output["fetchCalls"] == [
        "/api/identity/personal",
        "/api/network/ledger/heads?org=module-load-org",
        "/api/network/binding?org=module-load-org",
        "/api/network/rekey-policy?org=module-load-org",
        "/api/network/serve-cert?org=module-load-org",
        "/api/network/membership-checkpoint/decision"
        "?org=module-load-org"
        "&persona=" + persona.public_hex +
        "&genesis_id=" + genesis_id,
    ]

    entry = output["signOnResult"]["orgs"][0]
    assert entry["personaPub"] == persona.public_hex
    certificate = DelegationCert.from_json(entry["certWire"])
    verify_chain(
        certificate,
        persona.public_hex,
        org=org_uuid,
        required_scope="link:publish",
    )
    assert certificate.subject.id == persona.public_hex
    envelope = output["envelope"]
    verify_signature(
        envelope["signer"],
        envelope["sig"],
        request_signing_input(
            "TUNNEL",
            "/control/create-link",
            envelope["ts"],
            envelope["signer"],
            envelope["payload"],
        ),
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_node_named_imports_stay_window_free_and_unconfigured():
    result = subprocess.run(
        ["node", str(HARNESS), "node"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    output = json.loads(result.stdout)
    assert output["exports"] == [
        "configure",
        "signOn",
        "signRegistryRequest",
    ]
    assert output["windowType"] == "undefined"
    assert "no live operator session key" in output["rejection"]
