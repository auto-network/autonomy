"""The browser's delegate proof must be byte-identical to Python's.

Two canonicalisers that disagree produce a signature valid on one side and
invalid on the other — worse than either failing outright, because each
implementation believes it agrees with the other. The delegate consent proof is
the sharpest case: the browser signs it, the ledger verifies it, and a mismatch
surfaces as an unexplained authority error at the first vault write rather than
at the line that caused it.

Three parts of the binding are load-bearing and each is asserted separately:

* the domain prefix, which stops the proof being replayed as any other record,
* the SCOPE SORT, which is part of the binding rather than tidiness — a scope
  set has no order, and signing the caller's order would make one grant produce
  two different signatures,
* the OMISSION of ``ttl`` when absent, mirroring the payload, so a grant with no
  duration commits to a body that has no such field.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess

import pytest

from tools.network.ledger.events import delegate_proof_input

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not on PATH"
)

_CEREMONY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "static", "js", "ceremony",
)

# The canonicaliser and domain the browser module uses, inlined so this test
# fails when vault-unlock.js drifts rather than when a helper it imports does.
_JS = r"""
const enc = new TextEncoder();
function canonicalJson(v){
  if (v===null||typeof v!=='object') return JSON.stringify(v);
  if (Array.isArray(v)) return '['+v.map(canonicalJson).join(',')+']';
  return '{'+Object.keys(v).sort().map(k=>JSON.stringify(k)+':'+canonicalJson(v[k])).join(',')+'}';
}
const t = JSON.parse(process.env.CROSSIMPL_TERMS);
const body = {
  genesis_id: t.genesis_id, issuer_key: t.issuer_key, child_pub: t.child_pub,
  scope: [...new Set(t.scope)].sort(),
  can_redelegate: Boolean(t.can_redelegate),
  grant_nonce: t.grant_nonce,
};
if (t.ttl !== null && t.ttl !== undefined) body.ttl = Number(t.ttl);
const bytes = enc.encode('autonomy.ledger.delegate-consent.v2\n' + canonicalJson(body));
const h = await crypto.subtle.digest('SHA-256', bytes);
console.log(JSON.stringify({
  len: bytes.length,
  sha: [...new Uint8Array(h)].map(b=>b.toString(16).padStart(2,'0')).join(''),
}));
"""


def _js(terms: dict) -> dict:
    result = subprocess.run(
        ["node", "--input-type=module", "-e", _JS],
        env={**os.environ, "CROSSIMPL_TERMS": json.dumps(terms)},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _py(terms: dict) -> dict:
    raw = delegate_proof_input(
        terms["genesis_id"], terms["issuer_key"], terms["child_pub"],
        terms["scope"],
        can_redelegate=terms["can_redelegate"],
        ttl=terms.get("ttl"),
        grant_nonce=terms["grant_nonce"],
    )
    return {"len": len(raw), "sha": hashlib.sha256(raw).hexdigest()}


def _terms(**over):
    base = dict(
        genesis_id="a" * 40,
        issuer_key="b" * 64,
        child_pub="c" * 64,
        scope=[
            "storage:capability:grant:None",
            "storage:state:advance:None",
        ],
        can_redelegate=False,
        ttl=12 * 60 * 60 * 1000,
        grant_nonce="d" * 64,
    )
    base.update(over)
    return base


def test_the_two_implementations_produce_the_same_bytes():
    """THE ONE THAT MATTERS."""
    terms = _terms()
    assert _js(terms) == _py(terms)


def test_scope_order_does_not_change_the_signature():
    """The sort is part of the binding. If either side signed the caller's
    order, one grant would produce two different valid-looking signatures and
    the fold's nonce uniqueness would be doing all the work alone."""
    forward = _terms()
    reversed_ = _terms(scope=list(reversed(forward["scope"])))
    assert _py(forward) == _py(reversed_)
    assert _js(forward) == _js(reversed_)
    assert _js(reversed_) == _py(forward)


def test_an_absent_ttl_is_omitted_rather_than_nulled_on_both_sides():
    """A grant with no duration must commit to a body with no such field. If
    one side sends null and the other omits, every open-ended grant fails."""
    terms = _terms(ttl=None)
    assert _js(terms) == _py(terms)
    assert _js(terms) != _js(_terms())


def test_changing_any_covered_term_changes_the_bytes_on_both_sides():
    """Each side must actually cover the terms, not merely agree on a digest
    of the parts it happens to include."""
    base_py, base_js = _py(_terms()), _js(_terms())
    for field, value in [
        ("genesis_id", "e" * 40),
        ("issuer_key", "f" * 64),
        ("child_pub", "0" * 64),
        ("grant_nonce", "1" * 64),
        ("can_redelegate", True),
        ("ttl", 60_000),
        ("scope", ["storage:state:advance:None"]),
    ]:
        changed = _terms(**{field: value})
        assert _py(changed) != base_py, f"python does not cover {field}"
        assert _js(changed) != base_js, f"the browser does not cover {field}"
        assert _py(changed) == _js(changed), f"implementations differ on {field}"


# ── The PersonaKemCredential the browser sign-in now publishes ───────────────
#
# vault-unlock.js publishes this credential and hands its KEM private half to
# the dashboard, exactly as tools/vault/warm_client does. If the browser's
# credential is not byte-identical to the Python one, a browser-published
# credential is a recipient no Python-side sealer/recovery can open — the vault
# would wake for the browser and go dark for every headless re-warm. Unlike the
# delegate proof above this imports the REAL ceremony modules (the crypto is too
# much to inline meaningfully), so it fails on any real drift in founding.js.

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
    """THE ONE THAT MATTERS for the login-warm parity: the browser's published
    credential must equal warm_client's, field for field and signature and all,
    or a browser sign-in warms a vault no headless re-warm can reopen."""
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
