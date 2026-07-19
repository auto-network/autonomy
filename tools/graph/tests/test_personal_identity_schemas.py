"""Tests for the personal identity + passkey schemas (onboarding bead).

Acceptance (model notes 53f65f2f-d73, 80ef5131-9f0; invariant I1):

* ``autonomy.identity.personal#1`` shares the org-key storage
  discipline: ONLY the canonical password-encrypted armor is storable,
  enforced at the schema layer so settings_ops / POST /api/graph/setting
  / the dashboard route are all covered by one gate. Plaintext-shaped
  and smuggled-armor payloads are tested rejections.
* ``autonomy.identity.passkey#1`` pins credential integrity: base64url
  wire forms, bounded lengths, a real 32-bit sign counter, a
  registrable-domain RP ID (passkeys are domain-bound), known transport
  hints only.
"""

from __future__ import annotations

import copy

import pytest

from tools.graph.schemas import personal_identity as pi
from tools.graph.schemas.registry import (
    SCHEMAS,
    SchemaValidationError,
    get_schema,
    validate_payload,
)
from tools.network.idkit import KeyPair
from tools.network.idkit.armor import encrypt_root_key

_ROOT = KeyPair.generate()
_ARMOR = encrypt_root_key(_ROOT, "week-glacier-thirty-nine", iterations=10_000)


def _personal_payload() -> dict:
    return {
        "armored_private_key": _ARMOR,
        "root_pub": _ROOT.public_hex,
        "display_name": "Alex",
        "created_at": "2026-07-19T00:00:00Z",
    }


def _passkey_payload() -> dict:
    return {
        "credential_id": "5Zzp7Y0aFRDV3v0eZkGmvKyVLp0",
        "public_key": "pQECAyYgASFYIC1nZ2t0aGVyZS1pcy1uby1zZWNyZXQtaGVyZSE"
                      "iWCB0aGlzLWlzLWEtcHVibGljLWtleS1vbmx5LWJsb2I",
        "sign_count": 0,
        "rp_id": "localhost",
        "origin": "https://localhost:8080",
        "label": "This device",
        "transports": ["internal", "hybrid"],
        "created_at": "2026-07-19T00:00:00Z",
    }


# ── registration ──────────────────────────────────────────────


def test_both_schemas_are_registered():
    keys = {str(k) for k in SCHEMAS}
    assert any(pi.PERSONAL_IDENTITY_SET_ID in k for k in keys)
    assert any(pi.PASSKEY_SET_ID in k for k in keys)


def test_valid_payloads_validate():
    validate_payload(pi.PERSONAL_IDENTITY_SET_ID, 1, _personal_payload())
    validate_payload(pi.PASSKEY_SET_ID, 1, _passkey_payload())


def test_personal_identity_is_distinct_from_org_key():
    """The personal root is its own set_id — NOT a row in the org-key
    set. One person ≠ one org (the sovereign model's whole point)."""
    assert pi.PERSONAL_IDENTITY_SET_ID != "autonomy.network.org-key"
    schema = get_schema(pi.PERSONAL_IDENTITY_SET_ID, 1)
    props = schema.export_json_schema()["properties"]
    assert "display_name" in props  # a person has a name; an org key doesn't


# ── personal identity: the I1 gate ────────────────────────────


def test_personal_rejects_plaintext_hex_key_material():
    payload = _personal_payload()
    payload["armored_private_key"] = _ROOT.private_hex
    with pytest.raises(SchemaValidationError, match="I1"):
        validate_payload(pi.PERSONAL_IDENTITY_SET_ID, 1, payload)


def test_personal_rejects_non_armor_text():
    payload = _personal_payload()
    payload["armored_private_key"] = "not an armor at all"
    with pytest.raises(SchemaValidationError, match="I1"):
        validate_payload(pi.PERSONAL_IDENTITY_SET_ID, 1, payload)


def test_personal_rejects_smuggled_armor_field():
    """An armor with an extra field is a smuggling channel (I1): the
    strict parse refuses anything beyond the canonical field set."""
    import base64
    import json

    from tools.network.idkit.armor import ARMOR_BEGIN, ARMOR_END, parse_armor

    data = parse_armor(_ARMOR)
    data["private_hex"] = _ROOT.private_hex
    body = base64.b64encode(json.dumps(data).encode()).decode()
    payload = _personal_payload()
    payload["armored_private_key"] = "\n".join([ARMOR_BEGIN, body, ARMOR_END])
    with pytest.raises(SchemaValidationError, match="I1"):
        validate_payload(pi.PERSONAL_IDENTITY_SET_ID, 1, payload)


def test_personal_rejects_non_canonical_armor_bytes():
    """Same fields, different byte layout → refused; storage carries ONE
    byte form so nothing can hide in formatting slack."""
    # Re-wrap the armor body at a different column width: parses fine,
    # but is not the canonical byte form.
    lines = _ARMOR.split("\n")
    b64 = "".join(lines[1:-1])
    rewrapped = "\n".join([lines[0]] +
                          [b64[i:i + 32] for i in range(0, len(b64), 32)] +
                          [lines[-1]])
    assert rewrapped != _ARMOR
    payload = _personal_payload()
    payload["armored_private_key"] = rewrapped
    with pytest.raises(SchemaValidationError, match="canonical"):
        validate_payload(pi.PERSONAL_IDENTITY_SET_ID, 1, payload)


def test_personal_rejects_mismatched_root_pub():
    payload = _personal_payload()
    payload["root_pub"] = "0" * 64
    with pytest.raises(SchemaValidationError, match="does not match"):
        validate_payload(pi.PERSONAL_IDENTITY_SET_ID, 1, payload)


@pytest.mark.parametrize("mutate,match", [
    (lambda p: p.pop("display_name"), "display_name"),
    (lambda p: p.update(display_name=""), "display_name"),
    (lambda p: p.update(display_name="x" * 121), "display_name"),
    (lambda p: p.pop("created_at"), "created_at"),
    (lambda p: p.update(created_at="yesterday"), "created_at"),
])
def test_personal_rejects_malformed_metadata(mutate, match):
    payload = _personal_payload()
    mutate(payload)
    with pytest.raises(SchemaValidationError, match=match):
        validate_payload(pi.PERSONAL_IDENTITY_SET_ID, 1, payload)


# ── passkey credential integrity ──────────────────────────────


@pytest.mark.parametrize("mutate,match", [
    (lambda p: p.pop("credential_id"), "credential_id"),
    (lambda p: p.update(credential_id="not/base64url+"), "base64url"),
    (lambda p: p.update(credential_id="AAAA=="), "base64url"),
    (lambda p: p.update(credential_id="A" * 1500), "credential_id"),
    (lambda p: p.pop("public_key"), "public_key"),
    (lambda p: p.update(public_key="has spaces"), "base64url"),
    (lambda p: p.pop("sign_count"), "sign_count"),
    (lambda p: p.update(sign_count=-1), "sign_count"),
    (lambda p: p.update(sign_count=2**32), "sign_count"),
    (lambda p: p.update(sign_count="0"), "sign_count"),
    (lambda p: p.update(sign_count=True), "sign_count"),
    (lambda p: p.pop("rp_id"), "rp_id"),
    (lambda p: p.update(rp_id="https://localhost"), "rp_id"),
    (lambda p: p.update(rp_id="localhost:8080"), "rp_id"),
    (lambda p: p.update(rp_id="127.0.0.1."), "rp_id"),
    (lambda p: p.update(rp_id="UPPER.example"), "rp_id"),
    (lambda p: p.pop("origin"), "origin"),
    (lambda p: p.update(origin="localhost:8080"), "origin"),
    (lambda p: p.update(transports=["telepathy"]), "transports"),
    (lambda p: p.update(transports="internal"), "transports"),
    (lambda p: p.update(aaguid="not-a-uuid"), "aaguid"),
    (lambda p: p.pop("created_at"), "created_at"),
])
def test_passkey_rejects_malformed_payloads(mutate, match):
    payload = copy.deepcopy(_passkey_payload())
    mutate(payload)
    with pytest.raises(SchemaValidationError, match=match):
        validate_payload(pi.PASSKEY_SET_ID, 1, payload)


def test_passkey_accepts_ts_net_rp_id():
    payload = _passkey_payload()
    payload["rp_id"] = "dash.tail1234.ts.net"
    payload["origin"] = "https://dash.tail1234.ts.net"
    validate_payload(pi.PASSKEY_SET_ID, 1, payload)


def test_passkey_optional_fields_are_optional():
    payload = _passkey_payload()
    for key in ("label", "transports", "aaguid"):
        payload.pop(key, None)
    validate_payload(pi.PASSKEY_SET_ID, 1, payload)
