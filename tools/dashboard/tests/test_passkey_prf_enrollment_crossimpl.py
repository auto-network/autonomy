"""Both PRF enrollment paths, driven through the passkey simulator, and the
provisioning key proven to open under Python's vault factor.

A promotable passkey's provisioning key is derived from the authenticator's PRF
output. Two kinds of real authenticator exist: one evaluates PRF at create()
(one gesture), one only reports support and needs a get() to actually evaluate
(two gestures). The ceremony has to handle both, and BOTH must produce the same
key for the same credential — and that key must be byte-identical to what
Python's `derive_encapsulation_keypair(prfOutput, VAULT_FACTOR_PURPOSE)`
produces, or a passkey the operator "promotes" is sealed to an address the
Python vault factor cannot open.

The whole ceremony runs headless against `authenticator-node.js`: no hardware,
no user gesture.
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
import { VirtualAuthenticator } from 'file://__CEREMONY__/authenticator-node.js';
import {
  evaluatePrf, prfEvalExtension, prfOutputFromResults, deriveProvisioningKey,
} from 'file://__CEREMONY__/enrollment.js';

const rpId = 'localhost', origin = 'https://localhost:8080';
const auth = new VirtualAuthenticator();
const hex = (u) => Buffer.from(u).toString('hex');
const prf = prfEvalExtension().prf;

// A) authenticator that evaluates PRF at create() — one gesture, no fallback.
const A = await auth.createCredential({
  rpId, origin, challenge: 'c1', credentialId: new Uint8Array(32).fill(0xa1),
  prf, evalAtCreate: true,
});
let aGetCalled = false;
const outA = await evaluatePrf(A.clientExtensionResults, async () => { aGetCalled = true; return {}; });
const keyA = (await deriveProvisioningKey(outA)).publicKeyHex;
// the SAME credential, evaluated via get(), must give the SAME output.
const aAsrt = await auth.getAssertion({ rpId, origin, challenge: 'c2', credentialId: A.rawId, prf });
const outAget = prfOutputFromResults(aAsrt.clientExtensionResults);

// B) authenticator that only reports support at create() — needs the fallback.
const B = await auth.createCredential({
  rpId, origin, challenge: 'c3', credentialId: new Uint8Array(32).fill(0xb2), prf: true,
});
let bGetCalled = false;
const outB = await evaluatePrf(B.clientExtensionResults, async () => {
  bGetCalled = true;
  const asrt = await auth.getAssertion({ rpId, origin, challenge: 'c4', credentialId: B.rawId, prf });
  return asrt.clientExtensionResults;
});
const keyB = (await deriveProvisioningKey(outB)).publicKeyHex;

// C) non-PRF authenticator — access-only, no provisioning key, no fallback tap.
const C = await auth.createCredential({
  rpId, origin, challenge: 'c5', credentialId: new Uint8Array(32).fill(0xc3), prf: false,
});
let cGetCalled = false;
const outC = await evaluatePrf(C.clientExtensionResults, async () => { cGetCalled = true; return {}; });

console.log(JSON.stringify({
  keyA, aGetCalled, createEqualsGetA: hex(outA) === hex(outAget), prfOutputA: hex(outA),
  keyB, bGetCalled, prfOutputB: hex(outB),
  accessOnly: outC, cGetCalled,
}));
"""


def _run() -> dict:
    result = subprocess.run(
        ["node", "--input-type=module", "-e", _JS.replace("__CEREMONY__", _CEREMONY)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_both_prf_paths_work_and_agree_and_open_under_python():
    r = _run()

    # A) PRF at create(): output obtained without a fallback assertion, and the
    # same credential's get() yields the identical output (deterministic salt).
    assert r["aGetCalled"] is False, "create-eval path must not need a get()"
    assert r["createEqualsGetA"] is True, "create and get outputs differ for one credential"

    # B) support-only at create(): the fallback get() ran exactly once.
    assert r["bGetCalled"] is True, "get() fallback did not run when create gave no output"

    # C) non-PRF: no provisioning key, and no wasted gesture.
    assert r["accessOnly"] is None, "a non-PRF passkey must yield no provisioning key"
    assert r["cGetCalled"] is False, "must not attempt a PRF get() on a non-PRF authenticator"

    # Cross-impl: the browser's provisioning key IS the Python vault factor's key.
    from tools.network.idkit.sealing import derive_encapsulation_keypair
    from tools.vault.factors import VAULT_FACTOR_PURPOSE

    for out_field, key_field in (("prfOutputA", "keyA"), ("prfOutputB", "keyB")):
        _, pub = derive_encapsulation_keypair(
            bytes.fromhex(r[out_field]), VAULT_FACTOR_PURPOSE,
        )
        assert pub == r[key_field], (
            f"browser and Python provisioning keys differ ({out_field}) — a "
            "promoted passkey would be unopenable"
        )
