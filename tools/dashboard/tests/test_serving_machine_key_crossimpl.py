"""The browser's per-org serving machine key must equal Python's, byte for byte.

The relay verifies the tunnel hello's ``machine_sig`` against the public half
of this key (registry/relay.py, SERVING_MACHINE_HELLO_DOMAIN) and, once an org
has any registered serving key, against its allow-set as well. The private half
exists only where the personal root is open — the operator's browser during the
unlock ceremony — so a single byte of drift between the two derivations is an
org whose connector starts, presents a key nobody expects, and cannot serve.

Imports the REAL ``ceremony/fleet-enrollment.js`` through node, so any drift in
the salt, the info encoding, or the HKDF parameters fails here.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not on PATH"
)

_CEREMONY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "static", "js", "ceremony",
)

_JS = r"""
import { mintFleetRuntimeCredential }
  from 'file://__CEREMONY__/fleet-enrollment.js';

const f = JSON.parse(process.env.SERVING_FIELDS);
const credential = await mintFleetRuntimeCredential({
  personalRootSeed: Uint8Array.from(Buffer.from(f.seed_hex, 'hex')),
  rootPub: f.root_pub,
  machineId: f.machine_id,
  machinePub: f.machine_pub,
  servingOrgs: f.serving_orgs,
});
console.log(JSON.stringify(credential.serving_machine_private_seeds ?? null));
"""


def _key(seed_hex: str):
    from tools.network.idkit.keys import KeyPair

    return KeyPair.from_private_hex(seed_hex)


def _fields():
    from tools.network.idkit.persona import derive_machine_key

    seed_hex = "31" * 32
    machine_id = "ab" * 32
    machine_key = derive_machine_key(bytes.fromhex(seed_hex), machine_id)
    return {
        "seed_hex": seed_hex,
        "root_pub": _key(seed_hex).public_hex,
        "machine_id": machine_id,
        "machine_pub": machine_key.public_hex,
        "serving_orgs": [
            {"org_uuid": "org-one", "genesis_id": "cd" * 32},
            {"org_uuid": "org-two", "genesis_id": "ef" * 32},
        ],
    }


def _js_seeds(fields: dict) -> dict:
    result = subprocess.run(
        ["node", "--input-type=module", "-e", _JS.replace("__CEREMONY__", _CEREMONY)],
        env={**os.environ, "SERVING_FIELDS": json.dumps(fields)},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_browser_and_python_derive_the_same_serving_seed():
    from tools.network.idkit.persona import derive_serving_machine_key

    fields = _fields()
    seeds = _js_seeds(fields)
    assert set(seeds) == {"org-one", "org-two"}
    for target in fields["serving_orgs"]:
        expected = derive_serving_machine_key(
            bytes.fromhex(fields["seed_hex"]),
            target["genesis_id"],
            fields["machine_id"],
        )
        derived = _key(seeds[target["org_uuid"]])
        assert derived.public_hex == expected.public_hex


def test_each_org_gets_a_different_key():
    """The whole point is unlinkability: two orgs on ONE machine must not be
    correlatable by their serving key."""
    seeds = _js_seeds(_fields())
    assert seeds["org-one"] != seeds["org-two"]


def test_no_serving_orgs_leaves_the_credential_untouched():
    """A machine serving no org must POST exactly what it posts today."""
    fields = _fields()
    fields["serving_orgs"] = []
    assert _js_seeds(fields) is None
