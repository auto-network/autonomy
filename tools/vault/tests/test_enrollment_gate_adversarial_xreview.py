"""Adversarial cross-review regression for the passkey-enrollment gate, attacked
at the VAULT SEALING SITES (``service.enroll_passkey_factor`` /
``service.enroll_into_class``).

Written and owned by an independent pillar (Test & Automation), on purpose: the
hole this closes — a passkey the personal root never signed becoming a vault
wrap recipient — existed originally because *nothing exercised the path*, so a
regression graded by the author of the code is the weak evidence this sprint set
out to stop relying on. Each case asserts a REFUSAL at the sealing site, so a
future vault change that stops routing an enrollment through
``verified_provisioning_key`` fails loudly here. The variants come straight from
platform's own attack list (fbdad5d6); #3 is the one whose safety could not be
self-graded — with ``row_key=None`` the row comparison is skipped, so the refusal
must come from the signature binding ``provisioning_public_key``, not from the
convenience check.

FORWARD SEAM (highest-value finding of this review, recorded so it is not lost to
the next person wiring the route): ``verify`` is only as strong as the
``root_pub`` a sealing site hands it. The fatal one-liner is populating
``root_pub`` from ``statement.signer`` — the check degenerates to
``signer == signer``, reads as verification, passes every test, and verifies
nothing. The passkey enroll path has no non-test caller yet; when it is wired,
``root_pub`` MUST come from the trusted personal identity, never from the
agent-writable settings row and never from the statement's own ``signer``.
Platform is tracking this as a precondition of the route work.
"""
from __future__ import annotations

import dataclasses

import pytest

from tools.network.idkit import KeyPair
from tools.network.idkit.enrollment import EnrollmentError, SignatureError, mint
from tools.vault import service
from tools.vault.store import VaultStore

_KW = dict(
    credential_id="Y3JlZGVudGlhbA",
    credential_public_key="a1" * 20,
    rp_id="localhost",
    origin="https://localhost:8080",
    nonce="ab" * 32,
    created_hlc=(1_755_600_000_000, 0),
    initial_sign_count=7,
)
_VICTIM_PROV = "aa" * 32
_ATTACKER_PROV = "bb" * 32


@pytest.fixture
def store():
    return VaultStore(":memory:")


@pytest.fixture
def victim():
    return KeyPair.generate()


@pytest.fixture
def attacker():
    return KeyPair.generate()


def _genuine(victim):
    return mint(root=victim, provisioning_public_key=_VICTIM_PROV, **_KW)


# ── enroll_passkey_factor (the sealing site) refuses every attack ───────────


def test_sealing_site_refuses_foreign_signer(store, victim, attacker):
    """#1 — a statement signed by an attacker-controlled root, presented with
    the victim's root_pub, is refused (signer must equal root_pub) and no
    passkey factor is stored."""
    forged = mint(root=attacker, provisioning_public_key=_ATTACKER_PROV, **_KW)
    with pytest.raises(SignatureError):
        service.enroll_passkey_factor(
            store, "evil", statement=forged, root_pub=victim.public_hex
        )
    with pytest.raises(Exception):
        store.get_published_factor("evil")  # nothing was persisted


def test_sealing_site_refuses_row_key_mismatch(store, victim):
    """#2 — a genuine statement whose row convenience-copy is the attacker's key
    is refused on mismatch."""
    with pytest.raises(EnrollmentError):
        service.enroll_passkey_factor(
            store, "dev", statement=_genuine(victim),
            root_pub=victim.public_hex, row_key=_ATTACKER_PROV,
        )


def test_sealing_site_refuses_tampered_key_with_row_key_none(store, victim):
    """#3a — the one that must not be self-graded. row_key=None skips the row
    comparison, so this can only be refused by the signature binding the
    provisioning key. Tampering it must break verification at the sealing site."""
    tampered = dataclasses.replace(_genuine(victim), provisioning_public_key=_ATTACKER_PROV)
    with pytest.raises(SignatureError):
        service.enroll_passkey_factor(
            store, "dev", statement=tampered, root_pub=victim.public_hex, row_key=None
        )


def test_sealing_site_refuses_provisioning_added_to_absent(store, victim):
    """#3b — a genuine non-PRF statement omits the provisioning key from the
    signed payload; adding one afterwards is caught by the signature."""
    no_prov = mint(root=victim, provisioning_public_key=None, **_KW)
    added = dataclasses.replace(no_prov, provisioning_public_key=_ATTACKER_PROV)
    with pytest.raises(SignatureError):
        service.enroll_passkey_factor(
            store, "dev", statement=added, root_pub=victim.public_hex, row_key=None
        )


# ── enroll_into_class (higher value: seals EVERY existing generation) ───────


def test_enroll_into_class_refuses_forged_passkey_before_extending(store, victim, attacker):
    """The higher-value sealing site: extending a class seals every existing
    generation to the new factor, so an unattested address there reaches
    everything already stored. A forged statement must be refused at the gate,
    before extend_class runs — so the refusal holds regardless of the openers."""
    service.enroll_password_factor(store, "pw-1", "alpha")
    class_id = service.create_policy_class(
        store, "password", ["pw-1"], created_at="t0")
    openers = {"pw-1": service.password_seed(store, "pw-1", "alpha")}

    forged = mint(root=attacker, provisioning_public_key=_ATTACKER_PROV, **_KW)
    before = store.get_class(class_id)
    with pytest.raises(SignatureError):
        service.enroll_into_class(
            store, class_id, openers, "evil-pk",
            new_passkey_statement=forged, root_pub=victim.public_hex,
        )
    # the class was not extended to the attacker
    assert store.get_class(class_id).factor_ids() == before.factor_ids()
