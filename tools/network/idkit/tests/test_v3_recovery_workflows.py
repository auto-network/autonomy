"""Every recovery-code workflow, proven cryptographically end to end on v3.

Not "does the function return something" — the ACTUAL flows: enroll, verify,
the recover-then-re-enroll ceremony (open root with the code, then re-establish
factors authorized BY that recovery-opened root, and prove the new armor opens
with the new factors and the old ones are gone), and update (rotate the code
with the old code + root). Design of record: graph://fd418706-97e.
"""

from __future__ import annotations

import pytest

from tools.network.idkit.keys import KeyPair
from tools.network.idkit.recovery import derive_recovery_factors, generate_recovery_code
from tools.network.idkit.root_factor_policy import (
    RootFactorPolicyError,
    add_recovery_slot,
    build_envelope,
    canonical_expression,
    create_password_factor,
    emit_armored_envelope,
    factor_leaf,
    open_envelope,
    open_root_with_recovery,
    parse_armored_envelope,
    passkey_factor,
    project_operations,
    recovery_recipient_public_key,
    replace_recovery_slot,
    FACTOR_RECIPIENT_PURPOSE,
)
from tools.network.idkit.sealing import derive_encapsulation_keypair


def _passkey(fid, cred, byte):
    seed = bytearray([byte] * 32)
    _, pub = derive_encapsulation_keypair(seed, FACTOR_RECIPIENT_PURPOSE)
    return passkey_factor(fid, cred, pub), seed


def _armor_with_recovery():
    """A v3 password-only armor carrying a recovery slot; returns everything."""
    root = KeyPair.generate()
    pw, pw_seed = create_password_factor(root.public_hex, "pw.old", "old-pass-old", iterations=10_000)
    env = build_envelope(root, generation=1, factors=[pw], access={"pw.old": pw_seed}, policy=factor_leaf("pw.old"))
    code = generate_recovery_code()
    env = add_recovery_slot(
        env, root_seed=bytes.fromhex(root.private_hex),
        recovery_recipient_pub=recovery_recipient_public_key(code),
        recovery_pub=derive_recovery_factors(code)["recovery_pub"],
    )
    return root, env, code, {"pw.old": pw_seed}


# ── ADD ─────────────────────────────────────────────────────────────────────
def test_workflow_add_then_the_code_opens_root():
    root, env, code, seeds = _armor_with_recovery()
    # armor round-trips through the wire form and the code still opens it
    armor = emit_armored_envelope(env)
    reparsed = parse_armored_envelope(armor)
    assert open_root_with_recovery(reparsed, code).private_hex == root.private_hex


# ── VERIFY ──────────────────────────────────────────────────────────────────
def test_workflow_verify_is_a_read_only_open():
    root, env, code, seeds = _armor_with_recovery()
    # verify = open with the code; it changes nothing and matches the root
    assert open_root_with_recovery(env, code).public_hex == root.public_hex
    # and the day-to-day factor still opens the SAME unchanged armor
    assert open_envelope(env, seeds).public_hex == root.public_hex
    with pytest.raises(RootFactorPolicyError):
        open_root_with_recovery(env, generate_recovery_code())


# ── RECOVER THEN RE-ENROLL (the real use) ───────────────────────────────────
def test_workflow_recover_then_reestablish_factors_authorized_by_the_code():
    root, env, code, seeds = _armor_with_recovery()

    # 1. lost the password + passkeys: open the root with the recovery code
    recovered = open_root_with_recovery(env, code)
    assert recovered.private_hex == root.private_hex

    # 2. on the factor screen, stage a brand-new password and a passkey, make
    #    the password root — commit it as a v3 transition authorized BY the
    #    recovery-opened root (the code IS the root authority for this session)
    new_pw, new_pw_seed = create_password_factor(root.public_hex, "pw.new", "fresh-pass-fresh", iterations=10_000)
    new_pk, new_pk_seed = _passkey("pk.new", "cred-new", 0x51)
    projected = project_operations(
        {"generation": env["generation"], "root_pub": env["root_pub"],
         "factors": env["factors"], "access": env["access"], "policy": env["policy"]},
        [
            {"op": "enroll_password", "factor": new_pw, "access": True},
            {"op": "enroll_passkey", "factor": new_pk, "access": True},
            {"op": "remove_factor", "factor_id": "pw.old"},
            {"op": "set_root_policy", "policy": {"op": "or", "children": [
                factor_leaf("pw.new"), factor_leaf("pk.new")]}},
        ],
    )
    # the new generation is built + signed with the recovered root seed, and we
    # carry the recovery slot forward so the code still works afterward
    rebuilt = build_envelope(
        recovered, generation=projected["generation"], factors=projected["factors"],
        access=projected["access"], policy=projected["root_policy"],
        recovery=env["recovery"],
    )

    # 3. PROOF: the new factors open the re-established armor…
    assert open_envelope(rebuilt, {"pw.new": new_pw_seed}).public_hex == root.public_hex
    assert open_envelope(rebuilt, {"pk.new": new_pk_seed}).public_hex == root.public_hex
    # …the OLD password no longer opens it (it was removed)…
    with pytest.raises(RootFactorPolicyError):
        open_envelope(rebuilt, {"pw.old": seeds["pw.old"]})
    # …and the recovery code still opens the root (carried forward)
    assert open_root_with_recovery(rebuilt, code).public_hex == root.public_hex


# ── UPDATE (rotate the code) ────────────────────────────────────────────────
def test_workflow_update_requires_old_code_and_root_and_swaps_which_code_works():
    root, env, old_code, seeds = _armor_with_recovery()
    new_code = generate_recovery_code()

    updated = replace_recovery_slot(
        env, old_code=old_code, root_seed=bytes.fromhex(root.private_hex),
        recovery_recipient_pub=recovery_recipient_public_key(new_code),
        recovery_pub=derive_recovery_factors(new_code)["recovery_pub"],
    )
    # the NEW code opens the root; the OLD code no longer does
    assert open_root_with_recovery(updated, new_code).public_hex == root.public_hex
    with pytest.raises(RootFactorPolicyError):
        open_root_with_recovery(updated, old_code)
    # day-to-day factors are untouched by a code rotation
    assert open_envelope(updated, seeds).public_hex == root.public_hex


def test_workflow_update_refuses_a_wrong_old_code():
    root, env, old_code, seeds = _armor_with_recovery()
    new_code = generate_recovery_code()
    with pytest.raises(RootFactorPolicyError):
        replace_recovery_slot(
            env, old_code=generate_recovery_code(),  # not the enrolled one
            root_seed=bytes.fromhex(root.private_hex),
            recovery_recipient_pub=recovery_recipient_public_key(new_code),
            recovery_pub=derive_recovery_factors(new_code)["recovery_pub"],
        )


def test_workflow_update_refuses_when_no_slot_exists():
    root = KeyPair.generate()
    pw, seed = create_password_factor(root.public_hex, "pw", "p-p-p-p-p-p", iterations=10_000)
    env = build_envelope(root, generation=1, factors=[pw], access={"pw": seed}, policy=factor_leaf("pw"))
    code = generate_recovery_code()
    with pytest.raises(RootFactorPolicyError):
        replace_recovery_slot(
            env, old_code=code, root_seed=bytes.fromhex(root.private_hex),
            recovery_recipient_pub=recovery_recipient_public_key(code),
            recovery_pub=derive_recovery_factors(code)["recovery_pub"],
        )


# ── set_recovery operation (server commit flow) ─────────────────────────────
def test_set_recovery_op_projects_the_slot_and_carries_it_forward():
    root, env, code, seeds = _armor_with_recovery()
    # a base v3 armor with NO recovery, projected with a set_recovery op
    base_root = KeyPair.generate()
    pw, seed = create_password_factor(base_root.public_hex, "pw", "base-pass-pass", iterations=10_000)
    base = build_envelope(base_root, generation=1, factors=[pw], access={"pw": seed}, policy=factor_leaf("pw"))
    slot = env["recovery"]  # a real slot to plant
    projected = project_operations(
        {"generation": 1, "root_pub": base["root_pub"], "factors": base["factors"],
         "access": base["access"], "policy": base["policy"]},
        [{"op": "set_recovery", "recovery": slot}],
    )
    assert projected["recovery"] == slot
    # an ordinary op batch on an armor that HAS a slot carries it forward untouched
    carried = project_operations(
        {"generation": env["generation"], "root_pub": env["root_pub"], "factors": env["factors"],
         "access": env["access"], "policy": env["policy"], "recovery": env["recovery"]},
        [{"op": "set_access", "factor_id": "pw.old", "enabled": False}],
    )
    assert carried["recovery"] == env["recovery"]


def test_set_recovery_op_is_enroll_only_refusing_clear_and_overwrite():
    # operator ruling: the recovery code is NOT replaceable once generated.
    # set_recovery therefore refuses to clear an existing slot and refuses to
    # overwrite one — the only clear/re-enroll path (replacement without the old
    # code) is forbidden by construction.
    root, env, code, seeds = _armor_with_recovery()
    state = {"generation": env["generation"], "root_pub": env["root_pub"],
             "factors": env["factors"], "access": env["access"],
             "policy": env["policy"], "recovery": env["recovery"]}
    with pytest.raises(RootFactorPolicyError):
        project_operations(state, [{"op": "set_recovery", "recovery": None}])
    with pytest.raises(RootFactorPolicyError):
        project_operations(state, [{"op": "set_recovery", "recovery": env["recovery"]}])
