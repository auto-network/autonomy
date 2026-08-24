import copy

import pytest

from tools.network.idkit.keys import KeyPair
from tools.network.idkit.root_factor_policy import (
    FACTOR_RECIPIENT_PURPOSE,
    RootFactorPolicyError,
    build_envelope,
    canonical_expression,
    canonicalize_armored_envelope,
    create_password_factor,
    emit_armored_envelope,
    factor_leaf,
    factor_roles,
    grouped_mfa_policy,
    open_envelope,
    open_password_factor,
    parse_armored_envelope,
    passkey_factor,
    policy_satisfied,
    project_operations,
    validate_state,
)
from tools.network.idkit.sealing import derive_encapsulation_keypair


def _passkey(factor_id: str, credential_id: str, byte: int):
    seed = bytearray([byte] * 32)
    _, public = derive_encapsulation_keypair(seed, FACTOR_RECIPIENT_PURPOSE)
    return passkey_factor(factor_id, credential_id, public), seed


def _factors(root):
    password_a, seed_a = create_password_factor(
        root.public_hex, "password-a", "alpha", iterations=10_000,
    )
    password_b, seed_b = create_password_factor(
        root.public_hex, "password-b", "bravo", iterations=10_000,
    )
    passkey_a, seed_c = _passkey("passkey-a", "cred-a", 0x31)
    passkey_b, seed_d = _passkey("passkey-b", "cred-b", 0x42)
    return [password_a, password_b, passkey_a, passkey_b], {
        "password-a": seed_a,
        "password-b": seed_b,
        "passkey-a": seed_c,
        "passkey-b": seed_d,
    }


def test_grouped_policy_is_any_password_and_any_passkey():
    root = KeyPair.generate()
    factors, seeds = _factors(root)
    policy = grouped_mfa_policy(factors)
    envelope = build_envelope(
        root, generation=1, factors=factors, access=seeds, policy=policy,
    )

    for password in ("password-a", "password-b"):
        for passkey in ("passkey-a", "passkey-b"):
            opened = open_envelope(
                envelope, {password: seeds[password], passkey: seeds[passkey]},
            )
            assert opened.private_hex == root.private_hex

    for factor_id in seeds:
        with pytest.raises(RootFactorPolicyError):
            open_envelope(envelope, {factor_id: seeds[factor_id]})


def test_specific_subsets_exclude_other_valid_factors():
    root = KeyPair.generate()
    factors, seeds = _factors(root)
    policy = grouped_mfa_policy(
        factors, password_ids=["password-a"], passkey_ids=["passkey-b"],
    )
    envelope = build_envelope(
        root, generation=7, factors=factors, access=seeds, policy=policy,
    )
    assert open_envelope(
        envelope, {"password-a": seeds["password-a"], "passkey-b": seeds["passkey-b"]},
    ).public_hex == root.public_hex
    with pytest.raises(RootFactorPolicyError):
        open_envelope(
            envelope,
            {"password-b": seeds["password-b"], "passkey-b": seeds["passkey-b"]},
        )


def test_password_factor_is_a_stable_public_recipient():
    root = KeyPair.generate()
    factor, seed = create_password_factor(
        root.public_hex, "password-home", "correct horse", iterations=10_000,
    )
    opened = open_password_factor(root.public_hex, factor, "correct horse")
    try:
        assert opened == seed
    finally:
        opened[:] = b"\x00" * len(opened)
        seed[:] = b"\x00" * len(seed)
    with pytest.raises(RootFactorPolicyError, match="did not open"):
        open_password_factor(root.public_hex, factor, "wrong")


def test_one_logical_passkey_leaf_per_credential():
    root = KeyPair.generate()
    first, first_seed = _passkey("passkey-a", "shared-credential", 0x31)
    second, second_seed = _passkey("passkey-b", "shared-credential", 0x42)
    try:
        with pytest.raises(RootFactorPolicyError, match="one logical factor"):
            validate_state(
                [first, second],
                ["passkey-a", "passkey-b"],
                {"op": "or", "children": [
                    factor_leaf("passkey-a"), factor_leaf("passkey-b"),
                ]},
                root_pub=root.public_hex,
            )
    finally:
        first_seed[:] = b"\x00" * len(first_seed)
        second_seed[:] = b"\x00" * len(second_seed)


def test_signature_gates_use_before_any_factor_open(monkeypatch):
    root = KeyPair.generate()
    factors, seeds = _factors(root)
    envelope = build_envelope(
        root, generation=1, factors=factors, access=seeds,
        policy=grouped_mfa_policy(factors),
    )
    forged = copy.deepcopy(envelope)
    forged["access"] = ["password-a"]
    with pytest.raises(RootFactorPolicyError, match="signature"):
        open_envelope(
            forged,
            {"password-a": seeds["password-a"], "passkey-a": seeds["passkey-a"]},
        )


def test_expression_is_canonical_unique_and_roles_are_derived():
    policy = canonical_expression({
        "op": "and",
        "children": [
            {"op": "or", "children": [factor_leaf("password-b"), factor_leaf("password-a")]},
            {"op": "or", "children": [factor_leaf("passkey-b"), factor_leaf("passkey-a")]},
        ],
    })
    assert not policy_satisfied(policy, {"password-a"})
    assert policy_satisfied(policy, {"password-a", "passkey-b"})
    roles = factor_roles(
        policy,
        [{"factor_id": factor_id} for factor_id in (
            "password-a", "password-b", "passkey-a", "passkey-b", "spare",
        )],
        ["password-a", "spare"],
    )
    assert roles["password-a"] == {"access": "enabled", "root_role": "mfa-member"}
    assert roles["passkey-a"] == {"access": "disabled", "root_role": "mfa-member"}
    assert roles["spare"] == {"access": "enabled", "root_role": "none"}

    with pytest.raises(RootFactorPolicyError, match="more than once"):
        canonical_expression({
            "op": "or", "children": [factor_leaf("same"), factor_leaf("same")],
        })


def test_projected_state_rejects_unknown_members_and_accepts_access_only():
    root = KeyPair.generate()
    factors, _ = _factors(root)
    policy = factor_leaf("password-a")
    state = validate_state(
        factors, ["passkey-b"], policy, root_pub=root.public_hex,
    )
    assert state["roles"]["password-a"]["root_role"] == "individual"
    assert state["roles"]["passkey-b"] == {
        "access": "enabled", "root_role": "none",
    }
    with pytest.raises(RootFactorPolicyError, match="unknown factors"):
        validate_state(
            factors, [], factor_leaf("missing"), root_pub=root.public_hex,
        )


def test_batch_enables_grouped_mfa_as_one_change():
    root = KeyPair.generate()
    factors, _ = _factors(root)
    current = {
        "generation": 3,
        "root_pub": root.public_hex,
        "factors": factors,
        "access": [factor["factor_id"] for factor in factors],
        "policy": {
            "op": "or",
            "children": [factor_leaf(factor["factor_id"]) for factor in factors],
        },
    }
    projected = project_operations(current, [{
        "op": "set_root_policy", "policy": grouped_mfa_policy(factors),
    }])
    assert projected["generation"] == 4
    assert projected["change_count"] == 1
    assert set(projected["operations"]) == {"set_root_policy"}
    assert {
        role["root_role"] for role in projected["roles"].values()
    } == {"mfa-member"}


def test_batch_validates_final_result_not_transient_intermediate():
    root = KeyPair.generate()
    factors, _ = _factors(root)
    old = next(factor for factor in factors if factor["factor_id"] == "password-a")
    replacement, replacement_seed = create_password_factor(
        root.public_hex, "password-new", "new", iterations=10_000,
    )
    current = {
        "generation": 1,
        "root_pub": root.public_hex,
        "factors": factors,
        "access": [factor["factor_id"] for factor in factors],
        "policy": grouped_mfa_policy(
            factors, password_ids=[old["factor_id"]], passkey_ids=["passkey-a"],
        ),
    }
    target_policy = grouped_mfa_policy(
        [replacement, *factors],
        password_ids=["password-new"], passkey_ids=["passkey-a"],
    )
    projected = project_operations(current, [
        {"op": "remove_factor", "factor_id": "password-a"},
        {"op": "enroll_password", "factor": replacement, "access": True},
        {"op": "set_root_policy", "policy": target_policy},
    ])
    assert "password-a" not in {factor["factor_id"] for factor in projected["factors"]}
    assert projected["roles"]["password-new"]["root_role"] == "mfa-member"
    replacement_seed[:] = b"\x00" * len(replacement_seed)


def test_batch_refuses_last_group_member_removal():
    root = KeyPair.generate()
    factors, _ = _factors(root)
    policy = grouped_mfa_policy(
        factors, password_ids=["password-a"], passkey_ids=["passkey-a"],
    )
    current = {
        "generation": 1, "root_pub": root.public_hex, "factors": factors,
        "access": [factor["factor_id"] for factor in factors], "policy": policy,
    }
    with pytest.raises(RootFactorPolicyError, match="unknown factors"):
        project_operations(current, [{"op": "remove_factor", "factor_id": "password-a"}])


def test_access_only_non_prf_passkey_cannot_join_root_policy():
    root = KeyPair.generate()
    password, _ = create_password_factor(
        root.public_hex, "password-a", "pw", iterations=10_000,
    )
    access_only = passkey_factor("passkey-access", "cred-access", None)
    current = {
        "generation": 1,
        "root_pub": root.public_hex,
        "factors": [password, access_only],
        "access": ["password-a", "passkey-access"],
        "policy": factor_leaf("password-a"),
    }
    with pytest.raises(RootFactorPolicyError, match="without a derivable recipient"):
        project_operations(current, [{
            "op": "set_root_policy",
            "policy": {"op": "or", "children": [
                factor_leaf("password-a"), factor_leaf("passkey-access"),
            ]},
        }])


def test_same_cryptographic_factor_cannot_fill_two_policy_leaves():
    root = KeyPair.generate()
    seed = b"\x7a" * 32
    _, recipient = derive_encapsulation_keypair(seed, FACTOR_RECIPIENT_PURPOSE)
    first = passkey_factor("passkey-first", "cred-first", recipient)
    second = passkey_factor("passkey-second", "cred-second", recipient)
    with pytest.raises(RootFactorPolicyError, match="repeats one cryptographic recipient"):
        validate_state(
            [first, second], [],
            {"op": "and", "children": [
                factor_leaf("passkey-first"), factor_leaf("passkey-second"),
            ]},
            root_pub=root.public_hex,
        )


def test_synced_passkey_device_slots_share_credential_but_open_independently():
    root = KeyPair.generate()
    password, password_seed = create_password_factor(
        root.public_hex, "password-a", "alpha", iterations=10_000,
    )
    passkey, first_seed = _passkey("passkey-icloud", "icloud-credential", 0x51)
    second_slot, second_seed = _passkey("unused", "icloud-credential", 0x62)
    passkey["recipients"].append(second_slot["recipients"][0])
    factors = [password, passkey]
    policy = grouped_mfa_policy(
        factors,
        password_ids=[password["factor_id"]],
        passkey_ids=[passkey["factor_id"]],
    )
    envelope = build_envelope(
        root, generation=1, factors=factors, access=[], policy=policy,
    )
    for seed in (first_seed, second_seed):
        assert open_envelope(envelope, {
            password["factor_id"]: password_seed,
            passkey["factor_id"]: seed,
        }).public_hex == root.public_hex


def test_device_recipient_churn_does_not_change_passkey_policy_membership():
    root = KeyPair.generate()
    password, _ = create_password_factor(
        root.public_hex, "password-a", "alpha", iterations=10_000,
    )
    passkey, _ = _passkey("passkey-icloud", "icloud-credential", 0x51)
    second_slot, _ = _passkey("unused", "icloud-credential", 0x62)
    policy = grouped_mfa_policy([password, passkey])
    current = {
        "generation": 4,
        "root_pub": root.public_hex,
        "factors": [password, passkey],
        "access": ["password-a", "passkey-icloud"],
        "policy": policy,
    }
    added = project_operations(current, [{
        "op": "add_passkey_recipient",
        "factor_id": "passkey-icloud",
        "recipient": second_slot["recipients"][0],
    }])
    assert added["root_policy"] == policy
    assert len(next(
        factor for factor in added["factors"]
        if factor["factor_id"] == "passkey-icloud"
    )["recipients"]) == 2
    removed = project_operations({
        "generation": added["generation"],
        "root_pub": root.public_hex,
        "factors": added["factors"],
        "access": added["access"],
        "policy": added["root_policy"],
    }, [{
        "op": "remove_passkey_recipient",
        "factor_id": "passkey-icloud",
        "recipient_public_key": second_slot["recipients"][0]["recipient_public_key"],
    }])
    assert removed["root_policy"] == policy


def test_disable_mfa_atomically_promotes_selected_members():
    root = KeyPair.generate()
    factors, _ = _factors(root)
    current = {
        "generation": 9, "root_pub": root.public_hex, "factors": factors,
        "access": [factor["factor_id"] for factor in factors],
        "policy": grouped_mfa_policy(factors),
    }
    target = {
        "op": "or",
        "children": [factor_leaf("password-b"), factor_leaf("passkey-a")],
    }
    projected = project_operations(current, [{"op": "set_root_policy", "policy": target}])
    assert projected["roles"]["password-b"]["root_role"] == "individual"
    assert projected["roles"]["passkey-a"]["root_role"] == "individual"
    assert projected["roles"]["password-a"]["root_role"] == "none"


def test_v3_armor_round_trip_is_canonical_and_version_agnostic_helpers_read_it():
    from tools.network.idkit.armor import (
        armor_factor_types,
        armor_root_pub,
        canonicalize_armor,
    )

    root = KeyPair.generate()
    factors, _ = _factors(root)
    envelope = build_envelope(
        root, generation=2, factors=factors, access=[],
        policy=grouped_mfa_policy(factors),
    )
    armor = emit_armored_envelope(envelope)
    assert parse_armored_envelope(armor) == envelope
    assert canonicalize_armored_envelope(armor) == armor
    assert canonicalize_armor(armor) == armor
    assert armor_root_pub(armor) == root.public_hex
    assert sorted(armor_factor_types(armor)) == [
        "passkey", "passkey", "password", "password",
    ]
