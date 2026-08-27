"""Store-backed, headless acceptance for the policy-class lifecycle (crib §21).

Drives the same surface the CLI exposes — enroll, create, seal, open, enroll a
second factor, revoke, re-seal — through :mod:`tools.vault.service` against a
throwaway in-memory :class:`VaultStore` with throwaway test identities. No
browser, no human-entered password.
"""

from __future__ import annotations

import json
import pathlib
import re
from dataclasses import replace

import pytest

from tools.vault import service
from tools.vault.errors import ClassOpenError, VaultError
from tools.vault.store import VaultStore
from tools.vault.testkit import enroll_test_anchor
from tools.vault.testkit import make_test_genesis, make_test_identity


def _store():
    s = VaultStore(":memory:")
    enroll_test_anchor(s)
    return s


def _seeds(store, *identities):
    return {i.factor_id: service.password_seed(store, i.factor_id, i.password) for i in identities}


def test_full_headless_lifecycle():
    store = _store()
    genesis = make_test_genesis()
    a = make_test_identity(factor_id="pw-1", password="alpha")
    service.enroll_password_factor(store, a.factor_id, a.password)
    class_id = service.create_policy_class(store, "password", ["pw-1"], created_at="t0")

    # seal two settings; both name the one class
    cek1 = service.seal_setting(store, "s.a", class_id, genesis, "password")
    cek2 = service.seal_setting(store, "s.b", class_id, genesis, "password")
    assert service.open_setting(store, "s.a", _seeds(store, a)) == cek1
    assert service.open_setting(store, "s.b", _seeds(store, a)) == cek2

    # enroll a second factor: one added wrap, ciphertext byte-identical
    before = store.get_secret("s.a").sealed_cek
    service.enroll_into_class(store, class_id, _seeds(store, a), "pw-2", new_password="bravo")
    b = make_test_identity(factor_id="pw-2", password="bravo")  # id/password match the enrolled one
    assert store.get_secret("s.a").sealed_cek == before
    assert len(store.get_class(class_id).current().wraps) == 3   # pw-1 + pw-2 + root anchor

    # the enrolled factor opens both settings (shared class key)
    seeds_b = {"pw-2": service.password_seed(store, "pw-2", "bravo")}
    assert service.open_setting(store, "s.b", seeds_b) == cek2

    # rotate: revoke pw-1; survivors keep reading; both settings follow on re-seal
    service.revoke_and_rekey(store, class_id, "pw-1", created_at="t1")
    assert service.open_setting(store, "s.a", seeds_b) == cek1  # old gen still readable
    service.reseal_setting(store, "s.a", seeds_b)
    service.reseal_setting(store, "s.b", seeds_b)
    assert service.open_setting(store, "s.a", seeds_b) == cek1
    assert service.open_setting(store, "s.b", seeds_b) == cek2
    # both re-sealed onto the same current generation
    assert store.get_secret("s.a").sealed_cek["gen_id"] == store.get_class(class_id).current().gen_id
    assert store.get_secret("s.b").sealed_cek["gen_id"] == store.get_class(class_id).current().gen_id


def test_sealing_is_unattended_and_opening_still_requires_a_factor():
    """The class public key permits a write; only the read needs a factor."""
    store = _store()
    genesis = make_test_genesis()
    a = make_test_identity(factor_id="pw-1", password="alpha")
    service.enroll_password_factor(store, a.factor_id, a.password)
    class_id = service.create_policy_class(store, "password", ["pw-1"], created_at="t0")
    cek = service.seal_setting(store, "s.a", class_id, genesis, "password")
    with pytest.raises(ClassOpenError):
        service.open_setting(store, "s.a", {})
    assert service.open_setting(store, "s.a", _seeds(store, a)) == cek


def test_unattended_seal_upgrades_a_legacy_class_by_appending_a_generation():
    store = _store()
    genesis = make_test_genesis()
    a = make_test_identity(factor_id="pw-1", password="alpha")
    service.enroll_password_factor(store, a.factor_id, a.password)
    class_id = service.create_policy_class(
        store, "password", ["pw-1"], created_at="t0"
    )
    current = store.get_class(class_id)
    legacy_gen = replace(current.current(), sealing_public_key=None)
    legacy = replace(current, generations=(legacy_gen,))
    # Replace before the legacy form has ever been committed as a successor.
    store.db.execute(
        "UPDATE policy_classes SET wire = ? WHERE class_id = ?",
        (json.dumps(legacy.to_dict(), sort_keys=True), class_id),
    )
    store.db.commit()

    cek = service.seal_setting(
        store, "s.legacy", class_id, genesis, "password", created_at="t1"
    )
    upgraded = store.get_class(class_id)
    assert len(upgraded.generations) == 2
    assert upgraded.generations[0].to_dict() == legacy_gen.to_dict()
    assert upgraded.current().sealing_public_key is not None
    assert service.open_setting(store, "s.legacy", _seeds(store, a)) == cek


def test_store_round_trips_class_and_secret():
    store = _store()
    a = make_test_identity(factor_id="pw-1", password="alpha")
    service.enroll_password_factor(store, a.factor_id, a.password)
    class_id = service.create_policy_class(store, "password", ["pw-1"], created_at="t0")
    service.seal_setting(store, "s.a", class_id, make_test_genesis(), "password")
    reopened = VaultStore(store.path) if store.path != ":memory:" else store
    rec = reopened.get_class(class_id)
    assert rec.class_id == class_id and rec.policy == "password"
    sec = reopened.get_secret("s.a")
    assert sec.policy_class_id == class_id and sec.required_policy == "password"


# ── operator-facing text must not claim revocation removes access ──────────


_VAULT_DIR = pathlib.Path(__file__).resolve().parent.parent

# A revocation removes access to FUTURE writes, never to already-synced
# secrets (bead NOTES / crib §3). Any operator string saying otherwise is a
# correctness lie. Match the false claim in a few phrasings; the module's own
# docstrings state the opposite and must not trip these.
_FORBIDDEN = [
    re.compile(r"rev[a-z]*\b[^.\n]{0,60}\bremov[a-z]*\b[^.\n]{0,40}\baccess\b[^.\n]{0,40}\bexisting", re.I),
    re.compile(r"rev[a-z]*\b[^.\n]{0,60}\brevok[a-z]*\b[^.\n]{0,40}\baccess to existing secret", re.I),
    re.compile(r"lock[a-z]*\b[^.\n]{0,40}\bout of\b[^.\n]{0,40}\bexisting secret", re.I),
]


def test_no_operator_string_claims_revocation_removes_existing_access():
    offenders = []
    for path in _VAULT_DIR.rglob("*.py"):
        if "/tests/" in str(path):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pat in _FORBIDDEN:
            for m in pat.finditer(text):
                offenders.append(f"{path.name}: {m.group(0)!r}")
    assert not offenders, "operator text claims revocation removes existing access:\n" + "\n".join(offenders)


# ── a passkey's address must come from the root, not from the caller ────────
#
# Reported by the relay pillar, 2026-08-20: enroll_passkey_factor took a bare
# PublishedFactor and persisted its public key, and enroll_into_class then
# sealed every existing generation to it. Nothing consulted a statement.
#
# These pin the fixed shape rather than the fix: the unsafe call must be
# UNWRITABLE, not merely discouraged. A guard is forgotten at the next call
# site; a signature that cannot be expressed is not.


def _enrolment(root, *, prov, label="Test device"):
    from tools.network.idkit.enrollment import mint

    return mint(
        root=root,
        credential_id="Y3JlZA",
        credential_public_key="a1" * 20,
        rp_id="localhost",
        origin="https://localhost:8080",
        nonce="ab" * 32,
        created_hlc=(1_755_600_000_000, 0),
        initial_sign_count=0,
        label=label,
        provisioning_public_key=prov,
    )


def _passkey_root():
    from tools.network.idkit import KeyPair
    from tools.vault.factors import create_passkey_factor, random_seed

    root = KeyPair.generate()
    seed = random_seed()
    published = create_passkey_factor(seed, factor_id="pk-1")
    return root, seed, published


def test_a_bare_public_key_can_no_longer_be_enrolled():
    """THE REGRESSION. The old signature accepted a PublishedFactor — a public
    key with no provenance. Passing one must now fail to bind at all."""
    store = _store()
    _, _, published = _passkey_root()
    with pytest.raises(TypeError):
        service.enroll_passkey_factor(store, "pk-1", published)


def test_a_statement_enrols_the_address_it_attests():
    store = _store()
    root, _, published = _passkey_root()
    statement = _enrolment(root, prov=published.public_key)
    got = service.enroll_passkey_factor(
        store, "pk-1", statement=statement, root_pub=root.public_hex
    )
    assert got.public_key == published.public_key
    assert store.get_published_factor("pk-1").public_key == published.public_key


def test_a_statement_from_a_stranger_enrols_nothing():
    from tools.network.idkit import KeyPair

    store = _store()
    root, _, published = _passkey_root()
    stranger = KeyPair.generate()
    statement = _enrolment(stranger, prov=published.public_key)
    with pytest.raises(Exception):
        service.enroll_passkey_factor(
            store, "pk-1", statement=statement, root_pub=root.public_hex
        )
    with pytest.raises(VaultError):
        store.get_published_factor("pk-1")


def test_a_row_disagreeing_with_its_statement_enrols_nothing():
    """The address is the statement's, and the row's copy must agree with it —
    the attack is a row edited to an attacker key with the statement intact."""
    store = _store()
    root, _, published = _passkey_root()
    statement = _enrolment(root, prov=published.public_key)
    with pytest.raises(Exception):
        service.enroll_passkey_factor(
            store, "pk-1", statement=statement, root_pub=root.public_hex,
            row_key="ee" * 32,
        )
    with pytest.raises(VaultError):
        store.get_published_factor("pk-1")


def test_extending_a_class_seals_only_to_an_attested_address():
    """enroll_into_class is the higher-value target: it seals EVERY existing
    generation to the new factor, so an unattested address there reaches
    everything already stored, not only what is written next."""
    store = _store()
    genesis = make_test_genesis()
    a = make_test_identity(factor_id="pw-1", password="alpha")
    service.enroll_password_factor(store, a.factor_id, a.password)
    class_id = service.create_policy_class(store, "password", ["pw-1"], created_at="t0")
    cek = service.seal_setting(store, "s.a", class_id, genesis, "password")

    root, seed, published = _passkey_root()
    statement = _enrolment(root, prov=published.public_key)

    # a bare key is not an accepted shape any more
    with pytest.raises(TypeError):
        service.enroll_into_class(
            store, class_id, _seeds(store, a), "pk-1", new_passkey=published
        )
    # and a statement without a root to check it against is refused
    with pytest.raises(VaultError, match="root_pub"):
        service.enroll_into_class(
            store, class_id, _seeds(store, a), "pk-1",
            new_passkey_statement=statement,
        )
    assert service.open_setting(store, "s.a", _seeds(store, a)) == cek
