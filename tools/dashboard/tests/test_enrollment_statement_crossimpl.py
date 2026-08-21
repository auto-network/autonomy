"""The browser's passkey enrollment statement must be byte-identical to Python's.

The statement is the root's signed claim that a passkey is a legitimate
recipient; the server verifies it against the root from
``autonomy.identity.personal`` before ever sealing to the credential's
provisioning key. If the browser's ``mintEnrollmentStatement`` and Python's
``enrollment.mint`` disagree on a single canonical byte, the signature the
browser produced will not verify server-side — a passkey the operator enrolled
that no ceremony can ever promote. Two canonicalisers that disagree is worse
than either failing outright, because each side believes it agrees.

Imports the REAL ``ceremony/enrollment.js`` through node (the crypto is too much
to inline meaningfully), so it fails on any real drift in that module.
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
import { importEd25519RootSigningKey } from 'file://__CEREMONY__/primitives.js';
import { mintEnrollmentStatement } from 'file://__CEREMONY__/enrollment.js';

const f = JSON.parse(process.env.CROSSIMPL_FIELDS);
const key = await importEd25519RootSigningKey(
  Uint8Array.from(Buffer.from(f.seed_hex, 'hex')),
);
const st = await mintEnrollmentStatement(
  {
    credentialId: f.credential_id,
    credentialPublicKey: f.credential_public_key,
    rpId: f.rp_id,
    origin: f.origin,
    nonce: f.nonce,
    createdHlc: f.created_hlc,
    signer: f.signer,
    initialSignCount: f.initial_sign_count,
    provisioningPublicKey: f.provisioning_public_key ?? null,
    label: f.label ?? null,
    transports: f.transports ?? [],
    aaguid: f.aaguid ?? null,
  },
  key,
);
console.log(JSON.stringify(st));
"""


def _js(fields: dict) -> dict:
    result = subprocess.run(
        ["node", "--input-type=module", "-e", _JS.replace("__CEREMONY__", _CEREMONY)],
        env={**os.environ, "CROSSIMPL_FIELDS": json.dumps(fields)},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _py(fields: dict) -> dict:
    from tools.network.idkit import enrollment
    from tools.network.idkit.keys import KeyPair

    root = KeyPair.from_private_hex(fields["seed_hex"])
    st = enrollment.mint(
        root=root,
        credential_id=fields["credential_id"],
        credential_public_key=fields["credential_public_key"],
        rp_id=fields["rp_id"],
        origin=fields["origin"],
        nonce=fields["nonce"],
        created_hlc=tuple(fields["created_hlc"]),
        initial_sign_count=fields["initial_sign_count"],
        provisioning_public_key=fields.get("provisioning_public_key"),
        label=fields.get("label"),
        transports=tuple(fields.get("transports") or ()),
        aaguid=fields.get("aaguid"),
    )
    return st.to_dict()


def _signer(seed_hex: str) -> str:
    from tools.network.idkit.keys import KeyPair

    return KeyPair.from_private_hex(seed_hex).public_hex


def _fields(**over):
    seed = over.pop("seed_hex", "22" * 32)
    base = dict(
        seed_hex=seed,
        signer=_signer(seed),
        credential_id="AQIDBA",
        credential_public_key="a5010203",
        rp_id="localhost",
        origin="https://localhost:8080",
        nonce="ab" * 32,
        created_hlc=[1_755_000_000_000, 0],
        initial_sign_count=0,
        provisioning_public_key="cd" * 32,
        label="Test YubiKey",
        transports=["usb", "nfc"],
        aaguid="00000000-0000-0000-0000-000000000000",
    )
    base.update(over)
    base["signer"] = _signer(base["seed_hex"])
    return base


def test_a_prf_statement_is_byte_identical():
    """THE ONE THAT MATTERS: a promotable passkey's statement."""
    assert _js(_fields()) == _py(_fields())


def test_a_non_prf_statement_omits_the_provisioning_key_identically():
    """An access-only passkey: provisioning_public_key OMITTED, not nulled, on
    both sides — a statement that grows one later no longer matches."""
    fields = _fields(provisioning_public_key=None)
    js, py = _js(fields), _py(fields)
    assert "provisioning_public_key" not in js
    assert js == py


def test_optional_fields_are_omitted_identically_when_absent():
    fields = _fields(label=None, transports=[], aaguid=None)
    js, py = _js(fields), _py(fields)
    for k in ("label", "transports", "aaguid"):
        assert k not in js, f"{k} should be omitted when absent"
    assert js == py


def test_each_signed_field_changes_the_signature_on_both_sides():
    base = _py(_fields())
    for field, value in [
        ("seed_hex", "33" * 32),
        ("credential_id", "BQYHCA"),
        ("credential_public_key", "a5040506"),
        ("rp_id", "example.com"),
        ("origin", "https://example.com"),
        ("nonce", "cd" * 32),
        ("created_hlc", [1_755_000_000_001, 0]),
        ("initial_sign_count", 7),
        ("provisioning_public_key", "ef" * 32),
        ("label", "Other device"),
        ("transports", ["internal"]),
        ("aaguid", "11111111-1111-1111-1111-111111111111"),
    ]:
        changed = _fields(**{field: value})
        py = _py(changed)
        assert py != base, f"python does not cover {field}"
        assert _js(changed) == py, f"implementations differ on {field}"
