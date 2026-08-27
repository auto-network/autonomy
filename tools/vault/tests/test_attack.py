"""Cross-model attack regressions (bead auto-39d26 acceptance).

The construction was attacked by a DIFFERENT model before being consumed (bead:
"This construction is new and must be attacked before it is consumed"). The full
findings are in ``/workspace/output/attack_findings.md`` and summarized in
``experience_report.md``. Three findings BROKE; each is fixed and pinned here so
it cannot regress. Every other enumerated attack HELD at authoring; a
representative subset is pinned too, so a future change that reopens one fails
loudly.
"""

from __future__ import annotations

import os

import pytest

from tools.vault import (
    BOTH_POLICY,
    PASSWORD_POLICY,
    ClassOpenError,
    PolicyClassError,
    PolicyMismatchError,
    create_class,
    extend_class,
    open_cek,
    open_class,
    revoke_factor,
    seal_cek,
)
from tools.vault.errors import ConcurrencyError, FactorIndependenceError
from tools.vault.factors import (
    create_passkey_factor,
    create_password_factor,
    open_password_seed,
    random_seed,
)
from tools.vault.store import VaultStore
from tools.vault.testkit import enroll_test_anchor

GENESIS = "genesis-1"


def pw_factor(pw: str, fid: str):
    f = create_password_factor(pw, factor_id=fid)
    return f.published, open_password_seed(f.armor, pw)


# ── BROKEN-1: a `both` class must not collapse to 1-of-1 ────────────────────


def test_both_class_rejects_two_factors_sharing_key_material():
    """The exact attacker PoC: register ONE seed as both the password and the
    passkey factor of a `both` class. Their derived pubs are identical, so the
    class would open with that single secret. Minting must refuse it."""
    pub_pw, seed = pw_factor("solo-secret", "solo-pw")
    fake_passkey = create_passkey_factor(seed, factor_id="solo-pk")  # SAME seed
    assert fake_passkey.public_key == pub_pw.public_key  # the collapse precondition

    with pytest.raises(FactorIndependenceError):
        create_class(BOTH_POLICY, [pub_pw, fake_passkey], created_at="t0")


def test_extend_rejects_a_factor_reusing_existing_key_material():
    pub_pw, seed_pw = pw_factor("alpha", "b-pw")
    pk_seed = random_seed()
    pub_pk = create_passkey_factor(pk_seed, factor_id="b-pk")
    rec = create_class(BOTH_POLICY, [pub_pw, pub_pk], created_at="t0")
    # try to enroll a passkey whose seed is the password factor's seed
    collider = create_passkey_factor(seed_pw, factor_id="b-pk2")
    with pytest.raises(FactorIndependenceError):
        extend_class(rec, {"b-pw": seed_pw, "b-pk": pk_seed}, collider)


def test_single_wrap_class_also_rejects_duplicate_key_material():
    pub, seed = pw_factor("alpha", "pw-1")
    dup = create_password_factor("alpha", factor_id="pw-2")
    # force the duplicate to share pw-1's public key (same underlying seed)
    from tools.vault.factors import PublishedFactor

    dup_same = PublishedFactor("pw-2", "password", pub.public_key)
    with pytest.raises(FactorIndependenceError):
        create_class(PASSWORD_POLICY, [pub, dup_same], created_at="t0")


# ── BROKEN-2 / BROKEN-3: stale writes must be refused, not clobber ─────────


def _seed_store():
    store = VaultStore(":memory:")
    _, anchor = enroll_test_anchor(store)
    return store, anchor


def test_stale_extend_cannot_undo_a_revocation():
    """Writer A revokes pw-1; writer B, from a pre-revocation snapshot, extends.
    B's write drops A's appended generation and must be refused."""
    store, anchor = _seed_store()
    pub1, seed1 = pw_factor("alpha", "pw-1")
    pub2, seed2 = pw_factor("bravo", "pw-2")
    rec = create_class(PASSWORD_POLICY, [pub1], created_at="t0", recovery=anchor)
    rec = extend_class(rec, {"pw-1": seed1}, pub2)
    store.put_class(rec)

    stale = store.get_class(rec.class_id)  # B's snapshot (pw-1, pw-2)

    # A revokes pw-1 and commits
    store.put_class(revoke_factor(store.get_class(rec.class_id), "pw-1", created_at="t1"))
    assert "pw-1" not in store.get_class(rec.class_id).factor_ids()

    # B, racing from the stale snapshot, enrolls pw-3 — refused (drops the revoke gen)
    pub3, _seed3 = pw_factor("charlie", "pw-3")
    b_write = extend_class(stale, {"pw-1": seed1}, pub3)
    with pytest.raises(ConcurrencyError):
        store.put_class(b_write)

    # the revocation stands: pw-1 is not resurrected
    assert "pw-1" not in store.get_class(rec.class_id).factor_ids()


def test_stale_extend_cannot_lose_a_concurrent_enrollment():
    store, anchor = _seed_store()
    pub1, seed1 = pw_factor("alpha", "pw-1")
    rec = create_class(PASSWORD_POLICY, [pub1], created_at="t0", recovery=anchor)
    store.put_class(rec)

    base = store.get_class(rec.class_id)  # both writers read this
    pub2, _ = pw_factor("bravo", "pw-2")
    pub3, _ = pw_factor("charlie", "pw-3")

    store.put_class(extend_class(base, {"pw-1": seed1}, pub2))  # A commits pw-2
    with pytest.raises(ConcurrencyError):  # B's stale write loses pw-2 → refused
        store.put_class(extend_class(base, {"pw-1": seed1}, pub3))

    assert "pw-2" in store.get_class(rec.class_id).factor_ids()


def test_append_only_successor_allows_legitimate_growth():
    store, anchor = _seed_store()
    pub1, seed1 = pw_factor("alpha", "pw-1")
    rec = create_class(PASSWORD_POLICY, [pub1], created_at="t0", recovery=anchor)
    store.put_class(rec)
    pub2, _ = pw_factor("bravo", "pw-2")
    store.put_class(extend_class(store.get_class(rec.class_id), {"pw-1": seed1}, pub2))
    store.put_class(revoke_factor(store.get_class(rec.class_id), "pw-1", created_at="t1"))
    assert store.get_class(rec.class_id).factor_ids() == ("pw-2",)


# ── MINOR: a zero-generation record fails in the package taxonomy ──────────


def test_zero_generation_record_raises_policy_error_not_indexerror():
    from tools.vault.policy_class import PolicyClassRecord

    with pytest.raises(PolicyClassError):
        PolicyClassRecord.from_dict(
            {"class_id": "c", "policy": "password", "generations": [], "created_at": "t0"}
        )


# ── a representative subset of the HELD attacks, pinned so they stay held ───


def test_held_cross_class_wrap_lifting_is_refused():
    """A wrap sealed under class A's id must not open when grafted into a record
    claiming class B's id (class_id is bound into the wrap purpose)."""
    import dataclasses

    pubA, seedA = pw_factor("alpha", "pw-1")
    recA = create_class(PASSWORD_POLICY, [pubA], created_at="t0")
    forged = dataclasses.replace(recA, class_id="different-class-id")
    with pytest.raises(ClassOpenError):
        open_class(forged, {"pw-1": seedA})


def test_held_both_downgrade_via_policy_tamper_is_refused():
    import dataclasses

    pub_pw, seed_pw = pw_factor("alpha", "b-pw")
    pk_seed = random_seed()
    pub_pk = create_passkey_factor(pk_seed, factor_id="b-pk")
    rec = create_class(BOTH_POLICY, [pub_pw, pub_pk], created_at="t0")
    forged = dataclasses.replace(rec, policy=PASSWORD_POLICY)  # lie: both→password
    with pytest.raises(ClassOpenError):
        open_class(forged, {"b-pw": seed_pw})  # a single factor must not open it


def test_held_suite_id_type_confusion_is_refused():
    pub, seed = pw_factor("alpha", "pw-1")
    rec = create_class(PASSWORD_POLICY, [pub], created_at="t0")
    sealed = seal_cek(rec, os.urandom(32), genesis_id=GENESIS, setting_name="s", required_policy=PASSWORD_POLICY)
    sealed["format"] = "unknown-public-seal"
    with pytest.raises(Exception):  # SuiteError / PolicyClassError — never opens
        open_cek(rec, {"pw-1": seed}, sealed, genesis_id=GENESIS, setting_name="s", required_policy=PASSWORD_POLICY)


def test_held_sealed_cek_tamper_is_refused():
    pub, seed = pw_factor("alpha", "pw-1")
    rec = create_class(PASSWORD_POLICY, [pub], created_at="t0")
    cek = os.urandom(32)
    sealed = seal_cek(rec, cek, genesis_id=GENESIS, setting_name="s", required_policy=PASSWORD_POLICY)
    ct = bytearray(bytes.fromhex(sealed["ciphertext"]))
    ct[0] ^= 0x01
    sealed["ciphertext"] = ct.hex()
    with pytest.raises(PolicyClassError):
        open_cek(rec, {"pw-1": seed}, sealed, genesis_id=GENESIS, setting_name="s", required_policy=PASSWORD_POLICY)


def test_held_generation_public_key_substitution_is_refused():
    import dataclasses

    pub, seed = pw_factor("alpha", "pw-1")
    rec = create_class(PASSWORD_POLICY, [pub], created_at="t0")
    sealed = seal_cek(
        rec, os.urandom(32), genesis_id=GENESIS, setting_name="s",
        required_policy=PASSWORD_POLICY,
    )
    other_pub, _ = pw_factor("beta", "pw-2")
    other = create_class(PASSWORD_POLICY, [other_pub], created_at="t0")
    forged_gen = dataclasses.replace(
        rec.current(), sealing_public_key=other.current().sealing_public_key,
    )
    forged = dataclasses.replace(rec, generations=(forged_gen,))

    with pytest.raises(PolicyClassError, match="does not match"):
        open_cek(
            forged, {"pw-1": seed}, sealed, genesis_id=GENESIS,
            setting_name="s", required_policy=PASSWORD_POLICY,
        )
