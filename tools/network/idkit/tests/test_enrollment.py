"""What a passkey enrollment statement must refuse.

These tests are about one property: no path produces a sealing address the
personal root did not sign — at ingest from another machine, or at use, whenever
and however the row got where it is.

The per-field assertions are deliberate rather than a single "tampering is
caught" case. A binding that covers most of its fields looks identical to one
that covers all of them until the uncovered field is the one an attacker picks.
"""

from __future__ import annotations

import pytest

from tools.network.idkit import KeyPair
from tools.network.idkit.enrollment import (
    ENROLLMENT_VERSION,
    EnrollmentError,
    PasskeyEnrollmentStatement,
    mint,
    verified_provisioning_key,
    verify,
)
from tools.network.idkit.errors import SignatureError

PROV = "cd" * 32
ATTACKER = "ee" * 32


@pytest.fixture
def root():
    return KeyPair.generate()


def _kw():
    return dict(
        credential_id="Y3JlZGVudGlhbA",
        credential_public_key="a1" * 20,
        rp_id="localhost",
        origin="https://localhost:8080",
        nonce="ab" * 32,
        created_hlc=(1_755_600_000_000, 0),
        label="Jeremy's MacBook Pro",
        initial_sign_count=7,
        transports=("internal",),
        aaguid="fbfc3007-154e-4ecc-8c0b-6e020557d7bd",
    )


@pytest.fixture
def prf_statement(root):
    return mint(root=root, provisioning_public_key=PROV, **_kw())


@pytest.fixture
def plain_statement(root):
    """A non-PRF passkey: enrols, gates the dashboard, holds no key material."""
    return mint(root=root, **_kw())


# ── the happy paths, so the refusals below mean something ──


def test_a_prf_passkey_yields_its_sealing_address(root, prf_statement):
    assert prf_statement.prf_capable
    assert (
        verified_provisioning_key(prf_statement, root_pub=root.public_hex, row_key=PROV)
        == PROV
    )


def test_a_non_prf_passkey_enrols_with_no_provisioning_key(root, plain_statement):
    """auto-oox5r: both forms are supported. Absent is an ordinary optional
    field, NOT an 'unverified' state to be repaired later."""
    statement = verify(plain_statement, root_pub=root.public_hex)
    assert statement.prf_capable is False
    assert statement.provisioning_public_key is None


def test_the_wire_form_round_trips(root, prf_statement):
    assert verify(prf_statement.to_json(), root_pub=root.public_hex) == prf_statement


# ── the rule the module exists to enforce ──


def test_a_row_edited_to_an_attacker_key_produces_no_seal(root, prf_statement):
    """THE ONE THAT MATTERS. The statement is untouched and self-consistent;
    only the row's convenience copy was rewritten, which is exactly what a
    coopted local agent can do. Assert the NEGATIVE: no address comes back."""
    with pytest.raises(EnrollmentError, match="does not match the signed statement"):
        verified_provisioning_key(
            prf_statement, root_pub=root.public_hex, row_key=ATTACKER
        )


def test_a_non_prf_credential_cannot_be_asked_for_an_address(root, plain_statement):
    with pytest.raises(EnrollmentError, match="cannot receive a seal"):
        verified_provisioning_key(plain_statement, root_pub=root.public_hex, row_key=None)


def test_a_statement_signed_by_a_key_that_is_not_the_root_is_refused(prf_statement):
    """Self-consistent and correctly signed — by the wrong key. `signer` selects
    which root key after a rotation; it is never itself the reason to believe."""
    stranger = KeyPair.generate()
    forged = mint(root=stranger, provisioning_public_key=PROV, **_kw())
    with pytest.raises(SignatureError, match="not this identity's root"):
        verify(forged, root_pub=prf_statement.signer)


def test_a_non_prf_statement_cannot_GROW_a_provisioning_key(root, plain_statement):
    """Absence is bound, not merely permitted: the signed payload omits the
    field entirely, so adding one is a payload the root never signed."""
    grown = plain_statement.to_dict()
    grown["provisioning_public_key"] = ATTACKER
    with pytest.raises(SignatureError):
        verify(grown, root_pub=root.public_hex)


# ── every covered field, one at a time ──


@pytest.mark.parametrize(
    "field,value",
    [
        ("credential_id", "b3RoZXI"),
        ("credential_public_key", "b2" * 20),
        ("rp_id", "evil.example"),
        ("origin", "https://evil.example"),
        ("nonce", "ff" * 32),
        ("signer", "ff" * 32),
        ("provisioning_public_key", ATTACKER),
        ("created_hlc", [1, 1]),
        ("version", ENROLLMENT_VERSION + 1),
        # confirmed unsigned by the Mission Control pillar review, 2026-08-20
        ("label", "Jeremy's YubiKey"),
        ("initial_sign_count", 0),
        ("transports", ["usb"]),
        ("aaguid", "00000000-0000-0000-0000-000000000000"),
    ],
)
def test_every_signed_field_is_actually_covered(root, prf_statement, field, value):
    tampered = prf_statement.to_dict()
    tampered[field] = value
    with pytest.raises((SignatureError, EnrollmentError)):
        verify(tampered, root_pub=root.public_hex)


# ── structural refusals ──


@pytest.mark.parametrize(
    "field", ["credential_id", "credential_public_key", "rp_id", "origin", "nonce"]
)
def test_a_missing_field_is_refused_rather_than_defaulted(prf_statement, field):
    partial = prf_statement.to_dict()
    del partial[field]
    with pytest.raises(EnrollmentError, match="missing fields"):
        PasskeyEnrollmentStatement.from_dict(partial)


def test_an_unknown_field_is_refused(prf_statement):
    """A verifier that ignores extra fields lets an attacker smuggle one past
    a reader that later trusts it."""
    extra = prf_statement.to_dict()
    extra["provisioning_public_key_2"] = ATTACKER
    with pytest.raises(EnrollmentError, match="unknown statement fields"):
        PasskeyEnrollmentStatement.from_dict(extra)


def test_a_malformed_provisioning_key_is_refused_at_mint(root):
    with pytest.raises(EnrollmentError, match="provisioning_public_key"):
        mint(root=root, provisioning_public_key="not-hex", **_kw())


def test_the_domain_prefix_separates_this_from_other_signed_records(prf_statement):
    """A statement's signing input must not be replayable as any other record,
    so the domain prefix is part of what is signed, not framing around it."""
    assert prf_statement.signing_input().startswith(
        b"autonomy.identity.passkey-enrollment.v1\n"
    )


# ── what the operator READS is what the operator DECIDES on ──


def test_the_label_a_human_chooses_by_cannot_be_forged(root, prf_statement):
    """The attack this closes: the address is signed, so it cannot be swapped —
    but the operator never sees an address. They see a name, and they authorise
    a ceremony against the device that name refers to. Leave the name unsigned
    and the signature protects the half of the decision nobody makes."""
    relabelled = prf_statement.to_dict()
    relabelled["label"] = "Jeremy's YubiKey"
    with pytest.raises(SignatureError):
        verify(relabelled, root_pub=root.public_hex)


def test_a_statement_with_no_label_cannot_grow_one(root):
    """Same omission discipline as the provisioning key: a credential enrolled
    without a name commits to a payload that has none."""
    unnamed = mint(root=root, **{**_kw(), "label": None})
    assert verify(unnamed, root_pub=root.public_hex).label is None
    grown = unnamed.to_dict()
    grown["label"] = "Jeremy's MacBook Pro"
    with pytest.raises(SignatureError):
        verify(grown, root_pub=root.public_hex)


def test_the_counter_baseline_is_signed_but_the_live_counter_is_not(root, prf_statement):
    """A static statement can bind what the counter read at enrollment. It
    cannot bind the live one — that would need re-signing per assertion, which
    this record deliberately does not do. Anything checking monotonicity must
    compare against this floor rather than a previous row read."""
    assert verify(prf_statement, root_pub=root.public_hex).initial_sign_count == 7
    lowered = prf_statement.to_dict()
    lowered["initial_sign_count"] = 0
    with pytest.raises(SignatureError):
        verify(lowered, root_pub=root.public_hex)


def test_a_renamed_device_needs_a_new_statement(root):
    """The consequence of signing the label, asserted so it is not discovered
    later as a bug: you cannot edit a name in place."""
    original = mint(root=root, **_kw())
    renamed = mint(root=root, **{**_kw(), "label": "Work laptop"})
    assert original.signature != renamed.signature
    assert verify(renamed, root_pub=root.public_hex).label == "Work laptop"


# ── a boolean is not an integer, however hard Python insists ──


@pytest.mark.parametrize(
    "override",
    [
        {"version": True},
        {"created_hlc": (True, False)},
        {"initial_sign_count": True},
    ],
)
def test_a_correctly_signed_record_with_booleans_for_numbers_is_refused(root, override):
    """Reported by the relay pillar. `True == 1` and `isinstance(True, int)`, so
    a naive check accepts a record whose wire form carries `true` where a number
    belongs — correctly signed, so no signature check catches it.

    Not a forgery vector: the root signed it. It matters because a second
    implementation reading the same bytes is free to reject what this one
    accepts, and two verifiers that disagree while both believing they agree is
    the failure cross-implementation vectors exist to find.
    """
    from dataclasses import fields as dc_fields

    base = dict(
        version=ENROLLMENT_VERSION,
        credential_id="Yw",
        credential_public_key="a1" * 20,
        rp_id="localhost",
        origin="https://localhost:8080",
        nonce="ab" * 32,
        created_hlc=(1, 0),
        signer=root.public_hex,
        signature="0" * 128,
        provisioning_public_key=PROV,
        initial_sign_count=0,
    )
    base.update(override)
    draft = PasskeyEnrollmentStatement(**base)
    signed = PasskeyEnrollmentStatement(
        **{**{f.name: getattr(draft, f.name) for f in dc_fields(draft)},
           "signature": root.sign_hex(draft.signing_input())}
    )
    with pytest.raises(EnrollmentError, match="not a boolean"):
        verify(signed, root_pub=root.public_hex)
