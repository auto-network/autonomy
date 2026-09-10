"""Actual browser/Python parity for org KEM credentials and personal recipients."""

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

_KEM_JS = r"""
import { deriveKemSeed, buildPersonaKemCredential } from 'file://__CEREMONY__/founding.js';
import { derivePersona } from 'file://__CEREMONY__/ledger-event.js';

const t = JSON.parse(process.env.CROSSIMPL_KEM);
const rootSeed = Uint8Array.from(Buffer.from(t.root_seed_hex, 'hex'));
const persona = await derivePersona(rootSeed, t.genesis_id);
const kemSeed = await deriveKemSeed(rootSeed);
const { credential, kemPrivateKey } = await buildPersonaKemCredential({
  persona,
  genesisId: t.genesis_id,
  kemSeed,
  authorityHeads: t.heads,
  createdHlc: t.hlc,
});
console.log(JSON.stringify({ credential, kemPrivateKey }));
"""


def _js_credential(terms: dict) -> dict:
    result = subprocess.run(
        ["node", "--input-type=module", "-e", _KEM_JS.replace("__CEREMONY__", _CEREMONY)],
        env={**os.environ, "CROSSIMPL_KEM": json.dumps(terms)},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _py_credential(terms: dict) -> dict:
    from tools.network.idkit.persona import derive_persona
    from tools.network.storagekit import credentials as kem_credentials

    root_seed = bytes.fromhex(terms["root_seed_hex"])
    persona = derive_persona(root_seed, terms["genesis_id"])
    kem_seed = kem_credentials.derive_kem_seed(root_seed)
    cred, kem_private = kem_credentials.build(
        persona, terms["genesis_id"], kem_seed, terms["heads"], tuple(terms["hlc"]),
    )
    return {"credential": cred.to_dict(), "kemPrivateKey": kem_private}


def _kem_terms(**over):
    base = dict(
        root_seed_hex="11" * 32,
        genesis_id="a" * 64,
        heads=["b" * 64, "c" * 64],
        hlc=[1_755_000_000_000, 0],
    )
    base.update(over)
    return base


def test_the_kem_credential_is_byte_identical_across_impls():
    """Organization founding still publishes interoperable KEM credentials."""
    terms = _kem_terms()
    assert _js_credential(terms) == _py_credential(terms)


def test_the_credential_covers_every_input_on_both_sides():
    """Each side must actually bind every input, not agree on a digest of the
    parts it happens to include."""
    base = _py_credential(_kem_terms())
    for field, value in [
        ("root_seed_hex", "22" * 32),
        ("genesis_id", "d" * 64),
        ("heads", ["e" * 64]),
        ("hlc", [1_755_000_000_001, 0]),
    ]:
        changed = _kem_terms(**{field: value})
        py = _py_credential(changed)
        assert py != base, f"python credential does not cover {field}"
        assert _js_credential(changed) == py, f"implementations differ on {field}"


_AUDITED_DELEGATE_JS = r"""
import { deriveEncapsulationKeypair } from 'file://__CEREMONY__/primitives.js';
const root = Uint8Array.from(Buffer.from(process.env.ROOT_SEED, 'hex'));
const pair = await deriveEncapsulationKeypair(
  root, 'autonomy/vault/delegate-audited-recipient/v1');
console.log(JSON.stringify(pair));
"""


def test_audited_delegate_derivation_is_byte_identical_across_impls():
    """The browser handoff must name the same recipient Python seals to."""
    from tools.vault.personal_object import derive_delegate_audited_recipient

    root_hex = "42" * 32
    result = subprocess.run(
        ["node", "--input-type=module", "-e",
         _AUDITED_DELEGATE_JS.replace("__CEREMONY__", _CEREMONY)],
        env={**os.environ, "ROOT_SEED": root_hex},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    browser = json.loads(result.stdout)
    private_hex, public_hex = derive_delegate_audited_recipient(
        bytes.fromhex(root_hex)
    )
    assert browser == {
        "privateKeyHex": private_hex,
        "publicKeyHex": public_hex,
    }
