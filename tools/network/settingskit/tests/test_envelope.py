"""The settings envelope: addressed, byte-stable, unlinkable, fail-closed.

Acceptance for auto-4oxee (design of record graph://21a0da9e-1c2, drivers
D1/D10/D11). The cross-language byte assertions live in
tools/dashboard/tests/test_ceremony_settings_envelope.py; this file proves the
Python side's own contract.
"""

from __future__ import annotations

import json

import pytest

from tools.network.idkit import KeyPair, derive_persona
from tools.network.idkit.errors import SignatureError
from tools.network.settingskit import (
    ENVELOPE_FIELDS,
    EnvelopeFormatError,
    build_record,
    record_bytes,
    record_from_row,
    sign_record,
    signing_input,
    validate_record,
    verify_record,
)

SEED = bytes(range(32))
GENESIS_A = "11" * 32
GENESIS_B = "22" * 32
GENESIS_C = "33" * 32
SET_ID = "autonomy.org.member-directory"

PERSONA_A = derive_persona(SEED, GENESIS_A)
PERSONA_B = derive_persona(SEED, GENESIS_B)

WITNESS = {
    "entry": {
        "v": 2,
        "org": GENESIS_A,
        "seq": 3,
        "prev": "cc" * 32,
        "t": 1_755_500_000,
        "heads": {"authority": ["dd" * 32, "ee" * 32]},
        "publisher": "ab" * 32,
    },
    "entry_id": "12" * 32,
    "sig": "34" * 64,
}


def base_fields(**overrides) -> dict:
    fields = dict(
        org=GENESIS_A,
        set_id=SET_ID,
        key=PERSONA_A.public_hex,
        schema_revision=1,
        publication_state="published",
        deprecated=False,
        successor_id=None,
        payload={"display_name": "Ada ☃", "nested": [None, True, -7]},
        signed_at=1_755_500_000_123,
        signing_key=PERSONA_A.public_hex,
        witness=WITNESS,
    )
    fields.update(overrides)
    return fields


# --- every field is inside the bytes -----------------------------------------

MUTATIONS = {
    "org": GENESIS_B,
    "set_id": SET_ID + ".other",
    "key": PERSONA_B.public_hex,
    "schema_revision": 2,
    "publication_state": "canonical",
    "deprecated": True,
    "successor_id": "b2f6f0d6-retraction",
    "payload": {"display_name": "Ada", "nested": [None, True, -7]},
    "signed_at": 1_755_500_000_124,
    "signing_key": PERSONA_B.public_hex,
    "witness": {**WITNESS, "entry_id": "56" * 32},
}


def test_every_envelope_field_is_covered_by_the_bytes():
    """Eleven fields, eleven independent mutations, eleven byte changes."""
    assert set(MUTATIONS) == set(ENVELOPE_FIELDS)
    base = record_bytes(build_record(**base_fields()))
    for field, value in MUTATIONS.items():
        mutated = record_bytes(build_record(**base_fields(**{field: value})))
        assert mutated != base, f"{field} changed without changing the bytes"


def test_a_mutated_record_fails_the_base_signature():
    record = build_record(**base_fields())
    sig = sign_record(PERSONA_A, record)
    verify_record(record, sig)
    for field, value in MUTATIONS.items():
        with pytest.raises(SignatureError):
            verify_record(build_record(**base_fields(**{field: value})), sig)


# --- the encoding itself -----------------------------------------------------

def test_payload_key_order_and_whitespace_do_not_change_the_bytes():
    payload_text = json.dumps(
        base_fields()["payload"], ensure_ascii=False, indent=3, sort_keys=True
    )
    reparsed = build_record(**base_fields(payload=json.loads(payload_text)))
    assert record_bytes(reparsed) == record_bytes(build_record(**base_fields()))


def test_non_ascii_and_nested_payloads_round_trip():
    payload = {
        "name": "☃🚀 𐀀",
        "\U00010000": "astral key",
        "deep": {"list": [{"n": 9_007_199_254_740_991}, "文字"]},
    }
    record = build_record(**base_fields(payload=payload))
    encoded = record_bytes(record)
    assert encoded.isascii()
    assert json.loads(encoded)["payload"] == payload
    assert record_bytes(validate_record(json.loads(encoded))) == encoded


def test_no_witness_is_an_explicit_distinguishable_encoding():
    cited = record_bytes(build_record(**base_fields()))
    uncited = record_bytes(build_record(**base_fields(witness=None)))
    assert cited != uncited
    assert b'"witness":null' in uncited
    # A record missing the field entirely is not the null encoding.
    dropped = {
        k: v for k, v in build_record(**base_fields(witness=None)).items()
        if k != "witness"
    }
    with pytest.raises(EnvelopeFormatError):
        validate_record(dropped)


def test_supersedes_and_excludes_are_not_envelope_fields():
    """Organization rows use neither; the slot is fully inside the bytes."""
    assert "supersedes" not in ENVELOPE_FIELDS
    assert "excludes" not in ENVELOPE_FIELDS
    record = dict(build_record(**base_fields()), supersedes=None)
    with pytest.raises(EnvelopeFormatError):
        validate_record(record)


# --- refusals ----------------------------------------------------------------

def test_an_envelope_with_no_signing_key_is_refused_for_both_strategies():
    persona_shaped = {k: v for k, v in base_fields().items() if k != "signing_key"}
    delegate_shaped = {**persona_shaped, "key": "ops-policy"}
    for fields in (persona_shaped, delegate_shaped):
        with pytest.raises(TypeError):
            build_record(**fields)
        with pytest.raises(EnvelopeFormatError):
            validate_record({**fields})
        for bad in (None, "", "zz" * 32):
            with pytest.raises(EnvelopeFormatError):
                build_record(**fields, signing_key=bad)


@pytest.mark.parametrize(
    "field,value",
    [
        ("org", "org-slug"),
        ("org", GENESIS_A[:-2]),
        ("set_id", ""),
        ("key", ""),
        ("schema_revision", 0),
        ("schema_revision", True),
        ("publication_state", "draft"),
        ("deprecated", 0),
        ("deprecated", 1),
        ("successor_id", ""),
        ("payload", ["not", "an", "object"]),
        ("payload", {"pi": 3.14}),
        ("signed_at", -1),
        ("signed_at", True),
        ("witness", {}),
        ("witness", {"entry": {"t": 1.5}}),
    ],
)
def test_structural_defects_are_refused(field, value):
    with pytest.raises(EnvelopeFormatError):
        build_record(**base_fields(**{field: value}))


def test_signing_with_a_key_other_than_the_named_signer_is_refused():
    with pytest.raises(EnvelopeFormatError):
        sign_record(PERSONA_B, build_record(**base_fields()))


# --- D1: cross-organization unlinkability ------------------------------------

def test_zero_of_six_cross_org_pairs_verify_and_no_identity_field_recurs():
    rows = []
    for genesis in (GENESIS_A, GENESIS_B, GENESIS_C):
        persona = derive_persona(SEED, genesis)
        record = build_record(
            **base_fields(
                org=genesis,
                key=persona.public_hex,
                signing_key=persona.public_hex,
                witness=None,
            )
        )
        rows.append((genesis, record, sign_record(persona, record)))

    checked = 0
    for from_genesis, record, sig in rows:
        verify_record(record, sig)  # its own address
        for to_genesis, _, _ in rows:
            if to_genesis == from_genesis:
                continue
            checked += 1
            with pytest.raises(SignatureError):
                verify_record(dict(record, org=to_genesis), sig)
    assert checked == 6

    for identity_field in ("key", "signing_key"):
        values = {record[identity_field] for _, record, _ in rows}
        assert len(values) == 3, f"{identity_field} recurs across organizations"
    assert len({sig for _, _, sig in rows}) == 3


# --- the stored row is the record --------------------------------------------

def test_record_from_row_rebuilds_the_signed_bytes_exactly():
    record = build_record(**base_fields())
    sig = sign_record(PERSONA_A, record)
    row = {
        "set_id": record["set_id"],
        "key": record["key"],
        "schema_revision": record["schema_revision"],
        "publication_state": record["publication_state"],
        "deprecated": 0,
        "successor_id": None,
        "payload": json.dumps(record["payload"], ensure_ascii=False, indent=2),
        "signed_at": record["signed_at"],
        "signing_key": record["signing_key"],
        "witness": json.dumps(record["witness"]),
    }
    rebuilt = record_from_row(row, GENESIS_A)
    assert record_bytes(rebuilt) == record_bytes(record)
    verify_record(rebuilt, sig)


def test_a_tampered_stored_column_fails_verification():
    """publication_state and deprecated are statements, not mutable columns."""
    record = build_record(**base_fields())
    sig = sign_record(PERSONA_A, record)
    row = {
        "set_id": record["set_id"],
        "key": record["key"],
        "schema_revision": record["schema_revision"],
        "publication_state": record["publication_state"],
        "deprecated": 0,
        "successor_id": None,
        "payload": json.dumps(record["payload"]),
        "signed_at": record["signed_at"],
        "signing_key": record["signing_key"],
        "witness": json.dumps(record["witness"]),
    }
    for tamper in ({"publication_state": "canonical"}, {"deprecated": 1}):
        with pytest.raises(SignatureError):
            verify_record(record_from_row({**row, **tamper}, GENESIS_A), sig)


def test_signing_input_is_domain_separated():
    record = build_record(**base_fields())
    assert signing_input(record).startswith(
        b"autonomy.network.settings.envelope.v1\n"
    )
    assert signing_input(record)[38:] == record_bytes(record)
