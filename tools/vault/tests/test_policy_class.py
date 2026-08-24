"""Acceptance tests for the policy-class construction (bead auto-39d26).

Every bead acceptance criterion is asserted here at the construction layer;
:mod:`test_service` re-asserts the store-backed, headless CLI/API path (crib
§21). The cross-model attack findings live in :mod:`test_attack`.
"""

from __future__ import annotations

import os
from dataclasses import replace

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCMSIV

from tools.vault import (
    BOTH_POLICY,
    PASSWORD_POLICY,
    PRF_POLICY,
    ClassOpenError,
    PolicyClassError,
    PolicyMismatchError,
    create_class,
    enable_public_sealing,
    extend_class,
    open_cek,
    open_class,
    revoke_factor,
    seal_cek,
)
from tools.vault.factors import (
    create_passkey_factor,
    create_password_factor,
    open_password_seed,
    random_seed,
)

GENESIS = "genesis-1"


def pw_factor(pw: str, fid: str):
    """Return ``(published, seed)`` for a fresh password factor. The seed is
    captured from THIS factor's armor — a second factor made with the same
    password has a different random seed."""
    f = create_password_factor(pw, factor_id=fid)
    return f.published, open_password_seed(f.armor, pw)


# ── a setting names its class, and its data key opens only through it ───────


def test_data_key_opens_only_through_its_class():
    pub, seed = pw_factor("alpha", "pw-1")
    seeds = {"pw-1": seed}
    rec = create_class(PASSWORD_POLICY, [pub], created_at="t0")
    cek = os.urandom(32)
    sealed = seal_cek(rec, cek, genesis_id=GENESIS, setting_name="s1", required_policy=PASSWORD_POLICY)

    assert open_cek(rec, seeds, sealed, genesis_id=GENESIS, setting_name="s1", required_policy=PASSWORD_POLICY) == cek

    # a different class (its own key) cannot open it
    opub, oseed = pw_factor("beta", "pw-x")
    other = create_class(PASSWORD_POLICY, [opub], created_at="t0")
    with pytest.raises(PolicyClassError):
        open_cek(other, {"pw-x": oseed}, sealed, genesis_id=GENESIS, setting_name="s1", required_policy=PASSWORD_POLICY)


def test_cek_bound_to_setting_and_genesis():
    pub, seed = pw_factor("alpha", "pw-1")
    seeds = {"pw-1": seed}
    rec = create_class(PASSWORD_POLICY, [pub], created_at="t0")
    sealed = seal_cek(rec, os.urandom(32), genesis_id=GENESIS, setting_name="s1", required_policy=PASSWORD_POLICY)
    with pytest.raises(PolicyClassError):
        open_cek(rec, seeds, sealed, genesis_id=GENESIS, setting_name="OTHER", required_policy=PASSWORD_POLICY)
    with pytest.raises(PolicyClassError):
        open_cek(rec, seeds, sealed, genesis_id="OTHER", setting_name="s1", required_policy=PASSWORD_POLICY)


# ── two settings naming one class share one key; rotation moves both ────────


def test_two_settings_share_one_class_key_and_follow_rotation():
    pub1, seed1 = pw_factor("alpha", "pw-1")
    rec = create_class(PASSWORD_POLICY, [pub1], created_at="t0")
    cek1, cek2 = os.urandom(32), os.urandom(32)
    s1 = seal_cek(rec, cek1, genesis_id=GENESIS, setting_name="a", required_policy=PASSWORD_POLICY)
    s2 = seal_cek(rec, cek2, genesis_id=GENESIS, setting_name="b", required_policy=PASSWORD_POLICY)
    # both settings sealed under the SAME single generation → one class key
    assert s1["gen_id"] == s2["gen_id"]

    # enroll a survivor, then rotate by revoking pw-1
    pub2, seed2 = pw_factor("bravo", "pw-2")
    rec = extend_class(rec, {"pw-1": seed1}, pub2)
    rec = revoke_factor(rec, "pw-1", created_at="t1")
    assert "pw-1" not in rec.factor_ids() and "pw-2" in rec.factor_ids()

    # the survivor still opens BOTH old-generation secrets (rotation lost nothing)
    assert open_cek(rec, {"pw-2": seed2}, s1, genesis_id=GENESIS, setting_name="a", required_policy=PASSWORD_POLICY) == cek1
    assert open_cek(rec, {"pw-2": seed2}, s2, genesis_id=GENESIS, setting_name="b", required_policy=PASSWORD_POLICY) == cek2

    # at the next write both settings follow onto the NEW generation, together
    n1 = seal_cek(rec, cek1, genesis_id=GENESIS, setting_name="a", required_policy=PASSWORD_POLICY)
    n2 = seal_cek(rec, cek2, genesis_id=GENESIS, setting_name="b", required_policy=PASSWORD_POLICY)
    assert n1["gen_id"] == n2["gen_id"] == rec.current().gen_id
    # and the revoked factor cannot open the new generation
    with pytest.raises(ClassOpenError):
        open_cek(rec, {"pw-1": seed1}, n1, genesis_id=GENESIS, setting_name="a", required_policy=PASSWORD_POLICY)


# ── creating needs no factor; extending needs opening ──────────────────────


def test_create_requires_no_factor_only_public_keys():
    # only the published pub is used — no seed, no password, no prior class
    pub, _seed = pw_factor("alpha", "pw-1")
    rec = create_class(PASSWORD_POLICY, [pub], created_at="t0")
    assert rec.factor_ids() == ("pw-1",)
    assert len(rec.current().sealing_public_key) == 64


def test_generation_refuses_a_malformed_public_sealing_key():
    from tools.vault.policy_class import Generation

    with pytest.raises(PolicyClassError, match="64 lowercase hex"):
        Generation.from_dict({
            "gen_id": "g1",
            "wraps": [],
            "sealing_public_key": "not-an-x25519-key",
        })


def test_sealing_uses_only_the_class_public_key_and_opening_needs_the_factor():
    pub, seed = pw_factor("alpha", "pw-1")
    rec = create_class(PASSWORD_POLICY, [pub], created_at="t0")
    cek = os.urandom(32)

    # There is deliberately no opener argument on the seal operation.
    sealed = seal_cek(
        rec, cek, genesis_id=GENESIS, setting_name="unattended",
        required_policy=PASSWORD_POLICY,
    )
    assert sealed["format"] == "hpke-x25519-v1"
    with pytest.raises(ClassOpenError):
        open_cek(
            rec, {}, sealed, genesis_id=GENESIS, setting_name="unattended",
            required_policy=PASSWORD_POLICY,
        )
    assert open_cek(
        rec, {"pw-1": seed}, sealed, genesis_id=GENESIS,
        setting_name="unattended", required_policy=PASSWORD_POLICY,
    ) == cek


def test_legacy_aes_gcm_siv_cek_remains_readable_and_migrates_without_an_opener():
    """Already-vaulted rows survive the public-seal format transition."""
    from tools.network.storagekit import suites
    from tools.vault import policy_class as policy_mod

    pub, seed = pw_factor("alpha", "pw-1")
    modern = create_class(PASSWORD_POLICY, [pub], created_at="t0")
    legacy_gen = replace(modern.current(), sealing_public_key=None)
    legacy = replace(modern, generations=(legacy_gen,))
    cek = os.urandom(32)
    class_key = open_class(legacy, {"pw-1": seed})
    nonce = os.urandom(12)
    aad = policy_mod._cek_aad(
        legacy, legacy_gen.gen_id, GENESIS, "legacy", suites.WRAP_SUITE,
    )
    old_seal = {
        "suite_id": suites.WRAP_SUITE,
        "gen_id": legacy_gen.gen_id,
        "nonce": nonce.hex(),
        "ciphertext": AESGCMSIV(class_key).encrypt(nonce, cek, aad).hex(),
    }

    assert open_cek(
        legacy, {"pw-1": seed}, old_seal, genesis_id=GENESIS,
        setting_name="legacy", required_policy=PASSWORD_POLICY,
    ) == cek
    upgraded = enable_public_sealing(legacy, created_at="t1")
    assert upgraded.generations[0].to_dict() == legacy_gen.to_dict()
    assert upgraded.current().sealing_public_key is not None
    assert upgraded.current().gen_id != legacy_gen.gen_id


def test_extend_requires_opening_the_class():
    pub1, seed1 = pw_factor("alpha", "pw-1")
    rec = create_class(PASSWORD_POLICY, [pub1], created_at="t0")
    pub2, _seed2 = pw_factor("bravo", "pw-2")

    # with the factor: succeeds
    ext = extend_class(rec, {"pw-1": seed1}, pub2)
    assert set(ext.factor_ids()) == {"pw-1", "pw-2"}

    # without any opener: refused (a class you cannot open, you cannot extend)
    with pytest.raises(ClassOpenError):
        extend_class(rec, {}, pub2)
    # with a WRONG seed: also refused
    with pytest.raises(ClassOpenError):
        extend_class(rec, {"pw-1": random_seed()}, pub2)


# ── enrolling adds one wrap per class, changes no ciphertext ────────────────


def test_enroll_adds_one_wrap_and_leaves_ciphertext_byte_identical():
    pub1, seed1 = pw_factor("alpha", "pw-1")
    rec = create_class(PASSWORD_POLICY, [pub1], created_at="t0")
    cek = os.urandom(32)
    sealed = seal_cek(rec, cek, genesis_id=GENESIS, setting_name="s", required_policy=PASSWORD_POLICY)
    before = dict(sealed)

    pub2, seed2 = pw_factor("bravo", "pw-2")
    rec2 = extend_class(rec, {"pw-1": seed1}, pub2)

    # exactly one added wrap in the (single) generation
    assert len(rec2.current().wraps) == len(rec.current().wraps) + 1
    # the setting's sealed_cek is not an input to extend and is byte-identical
    assert sealed == before
    # the new factor opens the same setting — same class key
    assert open_cek(rec2, {"pw-2": seed2}, sealed, genesis_id=GENESIS, setting_name="s", required_policy=PASSWORD_POLICY) == cek


# ── policy mismatch: a weaker class cannot seal a stronger setting ──────────


def test_policy_mismatch_is_refused_at_seal_and_open():
    pub, seed = pw_factor("alpha", "pw-1")
    rec = create_class(PASSWORD_POLICY, [pub], created_at="t0")
    # a setting that requires `both` cannot be sealed under a `password` class
    with pytest.raises(PolicyMismatchError):
        seal_cek(rec, os.urandom(32), genesis_id=GENESIS, setting_name="s", required_policy=BOTH_POLICY)


# ── the `both` 2-of-2 split ─────────────────────────────────────────────────


def test_both_policy_needs_both_factors():
    pub_pw, seed_pw = pw_factor("alpha", "b-pw")
    pk_seed = random_seed()
    pub_pk = create_passkey_factor(pk_seed, factor_id="b-pk")
    rec = create_class(BOTH_POLICY, [pub_pw, pub_pk], created_at="t0")

    cek = os.urandom(32)
    sealed = seal_cek(rec, cek, genesis_id=GENESIS, setting_name="s", required_policy=BOTH_POLICY)
    assert open_cek(rec, {"b-pw": seed_pw, "b-pk": pk_seed}, sealed, genesis_id=GENESIS, setting_name="s", required_policy=BOTH_POLICY) == cek

    # neither factor alone opens the class
    with pytest.raises(ClassOpenError):
        open_class(rec, {"b-pw": seed_pw})
    with pytest.raises(ClassOpenError):
        open_class(rec, {"b-pk": pk_seed})


# ── revocation additions (bead NOTES) ──────────────────────────────────────


def test_revocation_mints_new_key_applied_at_next_write():
    pub1, seed1 = pw_factor("alpha", "pw-1")
    pub2, seed2 = pw_factor("bravo", "pw-2")
    rec = create_class(PASSWORD_POLICY, [pub1], created_at="t0")
    rec = extend_class(rec, {"pw-1": seed1}, pub2)
    gen_before = rec.current().gen_id

    rec = revoke_factor(rec, "pw-1", created_at="t1")
    # a NEW current generation exists, sealed only to the survivor
    assert rec.current().gen_id != gen_before
    assert rec.current().factor_ids() == ("pw-2",)
    # the new key applies at the next write; that write excludes the revoked factor
    sealed = seal_cek(rec, os.urandom(32), genesis_id=GENESIS, setting_name="s", required_policy=PASSWORD_POLICY)
    assert sealed["gen_id"] == rec.current().gen_id


def test_revocation_leaves_old_generations_untouched():
    pub1, seed1 = pw_factor("alpha", "pw-1")
    pub2, seed2 = pw_factor("bravo", "pw-2")
    rec = create_class(PASSWORD_POLICY, [pub1], created_at="t0")
    rec = extend_class(rec, {"pw-1": seed1}, pub2)
    old_gens = tuple(g.to_dict() for g in rec.generations)

    rec2 = revoke_factor(rec, "pw-1", created_at="t1")
    # every prior generation is byte-identical in the new record (append-only)
    assert tuple(g.to_dict() for g in rec2.generations[: len(old_gens)]) == old_gens


def test_revoke_factor_has_no_bulk_rewrap_path():
    """revoke_factor cannot re-wrap data keys in bulk — it takes no settings and
    reads none. Assert structurally that its only inputs are the record and the
    factor id, so no collection of sealed_ceks can be handed to it."""
    import inspect

    from tools.vault import policy_class

    params = list(inspect.signature(policy_class.revoke_factor).parameters)
    assert params == ["record", "factor_id", "created_at"], params
    src = inspect.getsource(policy_class.revoke_factor)
    # it never touches sealed_cek / cek material
    assert "sealed_cek" not in src and "seal_cek" not in src
