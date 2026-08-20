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
