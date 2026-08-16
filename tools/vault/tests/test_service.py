"""Store-backed, headless acceptance for the policy-class lifecycle (crib §21).

Drives the same surface the CLI exposes — enroll, create, seal, open, enroll a
second factor, revoke, re-seal — through :mod:`tools.vault.service` against a
throwaway in-memory :class:`VaultStore` with throwaway test identities. No
browser, no human-entered password.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from tools.vault import service
from tools.vault.errors import ClassOpenError, VaultError
from tools.vault.store import VaultStore
from tools.vault.testkit import make_test_genesis, make_test_identity


def _store():
    return VaultStore(":memory:")


def _seeds(store, *identities):
    return {i.factor_id: service.password_seed(store, i.factor_id, i.password) for i in identities}


def test_full_headless_lifecycle():
    store = _store()
    genesis = make_test_genesis()
    a = make_test_identity(factor_id="pw-1", password="alpha")
    service.enroll_password_factor(store, a.factor_id, a.password)
    class_id = service.create_policy_class(store, "password", ["pw-1"], created_at="t0")

    # seal two settings; both name the one class
    cek1 = service.seal_setting(store, "s.a", class_id, genesis, "password", _seeds(store, a))
    cek2 = service.seal_setting(store, "s.b", class_id, genesis, "password", _seeds(store, a))
    assert service.open_setting(store, "s.a", _seeds(store, a)) == cek1
    assert service.open_setting(store, "s.b", _seeds(store, a)) == cek2

    # enroll a second factor: one added wrap, ciphertext byte-identical
    before = store.get_secret("s.a").sealed_cek
    service.enroll_into_class(store, class_id, _seeds(store, a), "pw-2", new_password="bravo")
    b = make_test_identity(factor_id="pw-2", password="bravo")  # id/password match the enrolled one
    assert store.get_secret("s.a").sealed_cek == before
    assert len(store.get_class(class_id).current().wraps) == 2

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


def test_sealing_requires_a_factor_no_unattended_write():
    """No unattended process can write a secured setting (crib §18): sealing
    opens the class, and without a valid opener it is refused."""
    store = _store()
    genesis = make_test_genesis()
    a = make_test_identity(factor_id="pw-1", password="alpha")
    service.enroll_password_factor(store, a.factor_id, a.password)
    class_id = service.create_policy_class(store, "password", ["pw-1"], created_at="t0")
    with pytest.raises(ClassOpenError):
        service.seal_setting(store, "s.a", class_id, genesis, "password", {})


def test_store_round_trips_class_and_secret():
    store = _store()
    a = make_test_identity(factor_id="pw-1", password="alpha")
    service.enroll_password_factor(store, a.factor_id, a.password)
    class_id = service.create_policy_class(store, "password", ["pw-1"], created_at="t0")
    service.seal_setting(store, "s.a", class_id, make_test_genesis(), "password", _seeds(store, a))
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
