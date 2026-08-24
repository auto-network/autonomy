"""Browser/Python vectors for grouped personal-root factor policies."""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from tools.network.idkit import KeyPair
from tools.network.idkit.root_factor_policy import (
    FACTOR_RECIPIENT_PURPOSE,
    build_envelope,
    create_password_factor,
    emit_armored_envelope,
    grouped_mfa_policy,
    open_envelope,
    open_password_factor,
    parse_armored_envelope,
    passkey_factor,
)
from tools.network.idkit.sealing import derive_encapsulation_keypair


pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not on PATH"
)

_CEREMONY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "static", "js", "ceremony",
)

_JS = r"""
import {
  FACTOR_RECIPIENT_PURPOSE,
  createPasswordFactor, openPasswordFactor, parseFactorPolicyArmor,
  buildFactorPolicyArmor, openFactorPolicyArmor, signFactorPolicyTransition,
  policyDigest,
} from 'file://__CEREMONY__/root-factor-policy.js';
import {
  deriveEncapsulationKeypair, importEd25519RootSigningKey,
} from 'file://__CEREMONY__/primitives.js';

const a = JSON.parse(process.env.CROSSIMPL_ARGS);
const out = {};
if (a.op === 'create_password') {
  const made = await createPasswordFactor(a.root_pub, a.factor_id, a.password, 10000);
  out.factor = made.factor;
  out.seed_hex = Buffer.from(made.seed).toString('hex');
  made.seed.fill(0);
} else if (a.op === 'open') {
  const envelope = await parseFactorPolicyArmor(a.armor);
  const seeds = {};
  for (const [factorId, password] of Object.entries(a.passwords || {})) {
    const factor = envelope.factors.find((row) => row.factor_id === factorId);
    seeds[factorId] = await openPasswordFactor(envelope.root_pub, factor, password);
  }
  for (const [factorId, seedHex] of Object.entries(a.seeds || {})) {
    seeds[factorId] = Uint8Array.from(Buffer.from(seedHex, 'hex'));
  }
  const opened = await openFactorPolicyArmor(a.armor, seeds);
  out.seed_hex = Buffer.from(opened.seed).toString('hex');
  out.root_pub = opened.rootPub;
  opened.seed.fill(0);
  Object.values(seeds).forEach((seed) => seed.fill(0));
} else if (a.op === 'build') {
  const rootSeed = Uint8Array.from(Buffer.from(a.root_seed_hex, 'hex'));
  out.armor = await buildFactorPolicyArmor({
    rootSeed, rootPub: a.root_pub, generation: a.generation,
    factors: a.factors, access: a.access, policy: a.policy,
  });
  rootSeed.fill(0);
} else if (a.op === 'transition') {
  const seed = Uint8Array.from(Buffer.from(a.root_seed_hex, 'hex'));
  const signingKey = await importEd25519RootSigningKey(seed);
  out.signature = await signFactorPolicyTransition({
    signingKey, baseGeneration: a.base_generation,
    operations: a.operations, candidateArmor: a.candidate_armor,
  });
  seed.fill(0);
} else if (a.op === 'digest') {
  out.digest = await policyDigest(
    a.generation, a.root_pub, a.factors, a.access, a.policy,
  );
} else if (a.op === 'root_recipient') {
  const seed = Uint8Array.from(Buffer.from(a.seed_hex, 'hex'));
  const recipient = await deriveEncapsulationKeypair(seed, FACTOR_RECIPIENT_PURPOSE);
  out.public_key = recipient.publicKeyHex;
  out.purpose = FACTOR_RECIPIENT_PURPOSE;
  seed.fill(0);
}
console.log(JSON.stringify(out));
"""


def _js(arguments: dict) -> dict:
    result = subprocess.run(
        ["node", "--input-type=module", "-e", _JS.replace("__CEREMONY__", _CEREMONY)],
        env={**os.environ, "CROSSIMPL_ARGS": json.dumps(arguments)},
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _passkey(factor_id: str, credential_id: str, seed: bytes) -> dict:
    _, public = derive_encapsulation_keypair(seed, FACTOR_RECIPIENT_PURPOSE)
    return passkey_factor(factor_id, credential_id, public)


def test_root_recipient_derivation_is_browser_python_equal_and_vault_separated():
    seed = bytes(range(32))
    _, expected = derive_encapsulation_keypair(seed, FACTOR_RECIPIENT_PURPOSE)
    _, legacy_vault_recipient = derive_encapsulation_keypair(
        seed, "autonomy/vault-factor/v1",
    )
    actual = _js({"op": "root_recipient", "seed_hex": seed.hex()})
    assert actual == {
        "public_key": expected,
        "purpose": "autonomy/root-factor-recipient/v1",
    }
    assert expected != legacy_vault_recipient


def test_python_password_factor_opens_in_browser():
    root = KeyPair.generate()
    factor, seed = create_password_factor(
        root.public_hex, "password.primary", "planet-sparrow", iterations=10_000,
    )
    seed[:] = b"\x00" * len(seed)
    armor = emit_armored_envelope(build_envelope(
        root,
        generation=1,
        factors=[factor],
        access=["password.primary"],
        policy={"op": "factor", "factor_id": "password.primary"},
    ))
    result = _js({
        "op": "open", "armor": armor,
        "passwords": {"password.primary": "planet-sparrow"},
    })
    assert result == {"seed_hex": root.private_hex, "root_pub": root.public_hex}


def test_policy_digest_agrees_across_implementations():
    from tools.network.idkit.root_factor_policy import _policy_digest

    root = KeyPair.generate()
    factor, seed = create_password_factor(
        root.public_hex, "password.primary", "planet-sparrow", iterations=10_000,
    )
    seed[:] = b"\x00" * len(seed)
    policy = {"op": "factor", "factor_id": "password.primary"}
    expected = _policy_digest(1, root.public_hex, [factor], ["password.primary"], policy)
    actual = _js({
        "op": "digest", "generation": 1, "root_pub": root.public_hex,
        "factors": [factor], "access": ["password.primary"], "policy": policy,
    })["digest"]
    assert actual == expected


def test_browser_password_factor_opens_in_python():
    root = KeyPair.generate()
    made = _js({
        "op": "create_password", "root_pub": root.public_hex,
        "factor_id": "password.browser", "password": "violet-river",
    })
    seed = open_password_factor(root.public_hex, made["factor"], "violet-river")
    try:
        assert seed.hex() == made["seed_hex"]
    finally:
        seed[:] = b"\x00" * len(seed)


def test_python_grouped_any_password_and_any_passkey_opens_in_browser():
    root = KeyPair.generate()
    pw1, seed1 = create_password_factor(
        root.public_hex, "pw.one", "password-one", iterations=10_000,
    )
    pw2, seed2 = create_password_factor(
        root.public_hex, "pw.two", "password-two", iterations=10_000,
    )
    seed1[:] = b"\x00" * len(seed1)
    seed2[:] = b"\x00" * len(seed2)
    prf1, prf2 = b"\x41" * 32, b"\x42" * 32
    pk1 = _passkey("pk.one", "credential-one", prf1)
    pk2 = _passkey("pk.two", "credential-two", prf2)
    factors = [pw1, pw2, pk1, pk2]
    policy = grouped_mfa_policy(factors)
    armor = emit_armored_envelope(build_envelope(
        root, generation=7, factors=factors,
        access=[factor["factor_id"] for factor in factors], policy=policy,
    ))
    for password_id, password in (("pw.one", "password-one"), ("pw.two", "password-two")):
        for passkey_id, prf in (("pk.one", prf1), ("pk.two", prf2)):
            result = _js({
                "op": "open", "armor": armor,
                "passwords": {password_id: password},
                "seeds": {passkey_id: prf.hex()},
            })
            assert result["seed_hex"] == root.private_hex


def test_browser_grouped_armor_opens_in_python():
    root = KeyPair.generate()
    made = _js({
        "op": "create_password", "root_pub": root.public_hex,
        "factor_id": "pw.browser", "password": "browser-password",
    })
    prf = b"\x51" * 32
    passkey = _passkey("pk.python", "credential-python", prf)
    factors = [made["factor"], passkey]
    policy = grouped_mfa_policy(factors)
    armor = _js({
        "op": "build", "root_seed_hex": root.private_hex,
        "root_pub": root.public_hex, "generation": 3,
        "factors": factors, "access": ["pw.browser", "pk.python"],
        "policy": policy,
    })["armor"]
    envelope = parse_armored_envelope(armor)
    password_seed = open_password_factor(
        root.public_hex, made["factor"], "browser-password",
    )
    try:
        opened = open_envelope(envelope, {
            "pw.browser": password_seed,
            "pk.python": prf,
        })
        assert opened.private_hex == root.private_hex
    finally:
        password_seed[:] = b"\x00" * len(password_seed)


def test_browser_transition_signature_verifies_in_python():
    from tools.dashboard.identity_routes import _transition_message
    from tools.network.idkit.keys import verify_signature

    root = KeyPair.generate()
    factor, seed = create_password_factor(
        root.public_hex, "password.one", "one-password", iterations=10_000,
    )
    seed[:] = b"\x00" * len(seed)
    policy = {"op": "factor", "factor_id": "password.one"}
    armor = emit_armored_envelope(build_envelope(
        root, generation=1, factors=[factor], access=["password.one"], policy=policy,
    ))
    operations = [{"op": "set_root_policy", "policy": policy}]
    signature = _js({
        "op": "transition", "root_seed_hex": root.private_hex,
        "base_generation": 0, "operations": operations,
        "candidate_armor": armor,
    })["signature"]
    verify_signature(
        root.public_hex, signature, _transition_message(0, operations, armor),
    )
