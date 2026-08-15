"""Real module-load regression for browser sign-on adapter configuration."""

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
from tools.network.idkit.keys import verify_signature
from tools.network.registry.signing import request_signing_input

HARNESS = (
    Path(__file__).resolve().parent
    / "network_signon_autoconfigure_harness.mjs"
)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_browser_module_load_configures_real_signon_path():
    root = KeyPair.generate()
    org_uuid = str(uuid.uuid4())
    passphrase = "module load browser sign-on passphrase"
    armor = encrypt_root_key(root, passphrase, iterations=10_000)
    result = subprocess.run(
        ["node", str(HARNESS), "browser"],
        env={
            **os.environ,
            "AUTONOMY_ARMOR": armor,
            "AUTONOMY_ROOT_PUB": root.public_hex,
            "AUTONOMY_ORG_UUID": org_uuid,
            "AUTONOMY_PASSPHRASE": passphrase,
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    output = json.loads(result.stdout)

    assert output["state"]["signedIn"] is True
    assert output["storedSessions"] == 1
    assert output["subjectId"].startswith("browser-")
    assert output["fetchCalls"] == [
        "/api/network/org-key?org=module-load-org",
        "/api/network/binding?org=module-load-org",
        # A bound org checks whether its serving credential needs repair
        # before opening the root — best-effort, never blocks sign-on.
        "/api/network/serve-cert?org=module-load-org",
        # Persona-subject resolution probes the ledger; the 404 here means
        # "not founded", so the personal armor is never fetched and the
        # cert falls back to the label subject.
        "/api/network/ledger/heads?org=module-load-org",
    ]

    certificate = DelegationCert.from_json(
        output["signOnResult"]["certWire"]
    )
    verify_chain(
        certificate,
        root.public_hex,
        org=org_uuid,
        required_scope="link:publish",
    )
    envelope = output["envelope"]
    verify_signature(
        envelope["signer"],
        envelope["sig"],
        request_signing_input(
            "POST",
            "/v1/links",
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
