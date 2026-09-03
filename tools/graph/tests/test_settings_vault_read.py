"""Step six of ``read_set``: a vault setting, resolved back to its value.

Drives the real pipeline against a real key control — a founded throwaway org,
a persisted ``KeyControlStore``, a ``ContentStore`` on disk, real generation
keys — with the settings seams wired exactly as a host process wires them. The
only stand-in is the key holder, which is the seam this bead exists to define.

Two things these tests are watching for above all: that a member which does
not open resolves to a NAMED refusal rather than to a locator, to ciphertext,
or to absence; and that a member which does open carries no trace of having
been encrypted.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tools.graph import ops, schemas, settings_ops
from tools.graph.schemas.registry import SCHEMAS, UPCONVERTERS
from tools.graph.tests.vault_read_harness import VaultWorld, clear_seams
from tools.network.idkit.canonical import canonical_json
from tools.vault.errors import VaultError
from tools.vault.policy_class import open_cek
from tools.vault.store import VaultStore
from tools.vault.storage_object import (
    is_vault_locator,
    object_id_for,
    parse_locator,
)

SECRET = "sk-live-must-never-resolve-in-the-clear"
VAULT_SET = "autonomy.test.vault-read"
SECURED_SET = "autonomy.test.vault-read-secured"
PLAIN_SET = "autonomy.test.vault-read-plain"


# ── fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def graph_db_env(tmp_path, monkeypatch):
    db_path = tmp_path / "graph.db"
    monkeypatch.setenv("GRAPH_DB", str(db_path))
    monkeypatch.delenv("GRAPH_API", raising=False)
    yield db_path


@pytest.fixture(autouse=True)
def _isolate_schema_registry():
    schemas_snap = dict(SCHEMAS)
    upcon_snap = dict(UPCONVERTERS)
    try:
        yield
    finally:
        SCHEMAS.clear()
        SCHEMAS.update(schemas_snap)
        UPCONVERTERS.clear()
        UPCONVERTERS.update(upcon_snap)


@pytest.fixture(autouse=True)
def _clear_seams():
    clear_seams()
    try:
        yield
    finally:
        clear_seams()


@pytest.fixture
def vault_schema():
    @schemas.vaulted("audited")
    class VaultedV1(schemas.SettingSchema):
        set_id = VAULT_SET
        schema_revision = 1

    schemas.register_schema(VAULT_SET, 1, VaultedV1)
    return VaultedV1


@pytest.fixture
def secured_schema():
    @schemas.vaulted("secured")
    class SecuredV1(schemas.SettingSchema):
        set_id = SECURED_SET
        schema_revision = 1

    schemas.register_schema(SECURED_SET, 1, SecuredV1)
    return SecuredV1


@pytest.fixture
def plain_schema():
    class PlainV1(schemas.SettingSchema):
        set_id = PLAIN_SET
        schema_revision = 1

    schemas.register_schema(PLAIN_SET, 1, PlainV1)
    return PlainV1


@pytest.fixture
def vault(tmp_path):
    live = VaultWorld(tmp_path / "vault").register()
    try:
        yield live
    finally:
        live.close()


def member_of(set_id: str, key: str = "default"):
    return settings_ops.read_set(set_id, org=None).to_dict()[key]


def stored_payload(db_path: Path, setting_id: str):
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT payload FROM settings WHERE id = ?", (setting_id,)
        ).fetchone()
        assert row is not None
        return json.loads(row[0])
    finally:
        conn.close()


# ── the secret comes back ─────────────────────────────────────────────────


def test_a_vault_secret_resolves_to_plaintext_through_read_set(
    graph_db_env, vault_schema, vault
):
    """And the row it came from is still a locator, so the plaintext was
    produced by step six rather than by anything having stored it."""
    payload = {"access_token": SECRET, "refresh_token": "rt-also-secret"}
    setting_id = ops.add_setting(VAULT_SET, 1, "default", payload, org=ops.CALLER_ORG)

    assert is_vault_locator(stored_payload(graph_db_env, setting_id))
    assert member_of(VAULT_SET).payload == payload


def test_a_resolved_secret_carries_no_trace_of_having_been_encrypted(
    graph_db_env, vault_schema, plain_schema, vault
):
    """The caller's result is the assertion: a vault member and an ordinary
    member differ in their values and in nothing else. A field saying "this
    one was decrypted" would make the vault a mode every consumer has to
    know about, which is exactly what step six exists to avoid."""
    ops.add_setting(VAULT_SET, 1, "default", {"v": "secret"}, org=ops.CALLER_ORG)
    ops.add_setting(PLAIN_SET, 1, "default", {"v": "ordinary"}, org=ops.CALLER_ORG)

    vaulted = member_of(VAULT_SET).to_dict()
    plain = member_of(PLAIN_SET).to_dict()
    assert set(vaulted) == set(plain)
    assert "vault_error" not in vaulted
    assert "sealed_content_key" not in vaulted
    # Same for the serialized shape the API sends.
    over_the_wire = settings_ops.read_set(VAULT_SET, org=None).as_payload()
    assert set(over_the_wire["members"][0]) == set(plain)


def test_the_override_wins_and_what_opened_was_one_write_entire(
    graph_db_env, vault_schema, vault
):
    """A locator is a scalar, so a merge replaces it whole. The negative is
    the point: no field of the resolved locator came from the base row."""
    base_id = ops.add_setting(
        VAULT_SET, 1, "default", {"access_token": "one"}, org=ops.CALLER_ORG,
    )
    override_id = settings_ops.override_setting(
        base_id, {"access_token": "two"}, org=None,
    )

    base = parse_locator(stored_payload(graph_db_env, base_id))
    override = parse_locator(stored_payload(graph_db_env, override_id))
    assert base["revision_id"] != override["revision_id"]

    assert member_of(VAULT_SET).payload == {"access_token": "two"}
    merged = settings_ops.json_merge_patch(
        stored_payload(graph_db_env, base_id),
        stored_payload(graph_db_env, override_id),
    )
    # Every field of what step six opened came from the override row, and
    # none of it from the base — there is no splice to find.
    assert parse_locator(merged) == override
    assert not any(
        merged_value == base[name] and base[name] != override[name]
        for name, merged_value in parse_locator(merged).items()
    )


def test_a_partial_locator_cannot_be_expressed_by_an_override(
    graph_db_env, vault_schema, vault
):
    """Trying to override one field of the locator does not store a fragment:
    an override of a vaulted set re-seals its payload, so what lands is a
    whole locator naming a whole revision."""
    base_id = ops.add_setting(
        VAULT_SET, 1, "default", {"access_token": "one"}, org=ops.CALLER_ORG,
    )
    base = parse_locator(stored_payload(graph_db_env, base_id))
    override_id = settings_ops.override_setting(
        base_id, {"object_id": "an-object-nobody-wrote"}, org=None,
    )

    override = parse_locator(stored_payload(graph_db_env, override_id))
    assert override["object_id"] == base["object_id"], (
        "the object is derived from the setting's identity, not from a payload"
    )
    assert override["revision_id"] != base["revision_id"]
    assert member_of(VAULT_SET).payload == {"object_id": "an-object-nobody-wrote"}


# ── an earlier generation still opens ─────────────────────────────────────


def _write_under_an_earlier_generation(vault):
    """Write a secret, then force a new generation and hold only that one.

    Returns the earlier generation's state id. After this the reader holds a
    key that did not exist when the object was written, and reaching the
    object means walking the parent bridge backward.
    """
    ops.add_setting(
        VAULT_SET, 1, "default", {"access_token": SECRET}, org=ops.CALLER_ORG,
    )
    earlier = vault.initial.state_id

    leaving = vault.world.member(1)
    vault.world.grant(vault.author, leaving, vault.initial)
    vault.world.remove(leaving)
    # A write at a frontier carrying that contraction mints a generation in
    # line; the write itself is incidental here.
    ops.add_setting(
        VAULT_SET, 1, "later", {"access_token": "written-after"}, org=ops.CALLER_ORG,
    )
    current = [s for s in vault.world.held(vault.author) if s != earlier]
    assert current, "no new generation was minted"
    vault.hold_only(*current)
    return earlier


def test_an_object_written_under_an_earlier_generation_opens_through_bridges(
    graph_db_env, vault_schema, vault
):
    earlier = _write_under_an_earlier_generation(vault)
    resolved = settings_ops.read_set(VAULT_SET, org=None).to_dict()

    assert parse_locator(
        stored_payload(graph_db_env, resolved["default"].id)
    )["storage_state_id"] == earlier, "the object was not written under the old one"
    assert resolved["default"].payload == {"access_token": SECRET}
    assert resolved["default"].vault_error is None


# ── failing closed, four ways ─────────────────────────────────────────────


def test_no_key_holder_is_a_named_refusal_not_a_locator(
    graph_db_env, vault_schema, vault
):
    """The seam unfilled. Distinct from holding a key that does not reach:
    the fault is that nothing in this process holds the organization's key
    control at all."""
    ops.add_setting(
        VAULT_SET, 1, "default", {"access_token": SECRET}, org=ops.CALLER_ORG,
    )
    settings_ops.set_vault_key_holder(None)

    member = member_of(VAULT_SET)
    assert member.vault_error.reason == settings_ops.VAULT_NO_KEY_HOLDER
    assert member.payload is None
    assert not is_vault_locator(member.payload)
    assert SECRET not in json.dumps(member.to_dict())


def test_no_key_held_is_distinct_from_every_other_refusal(
    graph_db_env, vault_schema, vault
):
    ops.add_setting(
        VAULT_SET, 1, "default", {"access_token": SECRET}, org=ops.CALLER_ORG,
    )
    vault.hold_nothing()

    member = member_of(VAULT_SET)
    assert member.vault_error.reason == settings_ops.VAULT_NO_KEY_HELD
    assert member.payload is None


def test_a_missing_bridge_is_told_apart_from_holding_no_key(
    graph_db_env, vault_schema, vault
):
    """The storage layer refuses both the same way. They are different
    faults — one member was never granted anything, the other's bridge did
    not replicate — and an operator needs to know which."""
    _write_under_an_earlier_generation(vault)
    vault.drop_bridges()

    member = member_of(VAULT_SET)
    assert member.vault_error.reason == settings_ops.VAULT_MISSING_BRIDGE
    assert member.payload is None
    assert SECRET not in json.dumps(member.to_dict())


def test_a_failed_authenticated_decryption_is_its_own_refusal(
    graph_db_env, vault_schema, vault
):
    setting_id = ops.add_setting(
        VAULT_SET, 1, "default", {"access_token": SECRET}, org=ops.CALLER_ORG,
    )
    vault.tamper(
        parse_locator(stored_payload(graph_db_env, setting_id))["object_id"],
        "decryption_failed",
    )

    member = member_of(VAULT_SET)
    assert member.vault_error.reason == settings_ops.VAULT_DECRYPTION_FAILED
    assert member.payload is None


def test_an_unknown_suite_fails_closed_rather_than_negotiating_down(
    graph_db_env, vault_schema, vault
):
    setting_id = ops.add_setting(
        VAULT_SET, 1, "default", {"access_token": SECRET}, org=ops.CALLER_ORG,
    )
    vault.tamper(
        parse_locator(stored_payload(graph_db_env, setting_id))["object_id"],
        "unknown_suite",
    )

    member = member_of(VAULT_SET)
    assert member.vault_error.reason == settings_ops.VAULT_UNKNOWN_SUITE
    assert member.payload is None


def test_the_four_refusals_are_four_distinct_reasons(
    graph_db_env, vault_schema, vault
):
    """Asserted together, because "distinct" is a claim about the SET of
    them: four faults collapsing onto one reason would pass every test above
    and tell an operator nothing."""
    assert len({
        settings_ops.VAULT_NO_KEY_HELD,
        settings_ops.VAULT_DECRYPTION_FAILED,
        settings_ops.VAULT_MISSING_BRIDGE,
        settings_ops.VAULT_UNKNOWN_SUITE,
    }) == 4
    assert settings_ops.VAULT_NO_KEY_HOLDER not in {
        settings_ops.VAULT_NO_KEY_HELD,
        settings_ops.VAULT_DECRYPTION_FAILED,
        settings_ops.VAULT_MISSING_BRIDGE,
        settings_ops.VAULT_UNKNOWN_SUITE,
    }


def test_one_unopenable_secret_does_not_take_the_set_with_it(
    graph_db_env, vault_schema, vault
):
    """A resolver serves every consumer of a set at once. One secret that
    cannot be opened must cost that member and nothing else."""
    for key in ("alpha", "beta", "gamma"):
        ops.add_setting(VAULT_SET, 1, key, {"t": key}, org=ops.CALLER_ORG)
    broken = settings_ops.read_set(VAULT_SET, org=None).to_dict()["beta"]
    vault.tamper(
        parse_locator(stored_payload(graph_db_env, broken.id))["object_id"],
        "decryption_failed",
    )

    resolved = settings_ops.read_set(VAULT_SET, org=None).to_dict()
    assert sorted(resolved) == ["alpha", "beta", "gamma"], (
        "the refused member was dropped, which a caller reads as no such setting"
    )
    assert resolved["alpha"].payload == {"t": "alpha"}
    assert resolved["gamma"].payload == {"t": "gamma"}
    assert resolved["beta"].vault_error.reason == settings_ops.VAULT_DECRYPTION_FAILED


def test_a_refusal_survives_a_model_that_would_have_dropped_it(
    graph_db_env, vault_schema, vault
):
    """``model=`` drops members whose payload fails validation. A refusal has
    no payload to validate, and letting it through that filter would turn a
    named error back into an absence."""

    class Model:
        @classmethod
        def model_validate(cls, payload):
            raise ValueError("no payload validates here")

    ops.add_setting(
        VAULT_SET, 1, "default", {"access_token": SECRET}, org=ops.CALLER_ORG,
    )
    settings_ops.set_vault_key_holder(None)

    result = settings_ops.read_set(VAULT_SET, org=None, model=Model)
    assert len(result) == 1
    assert result.members[0].vault_error.reason == settings_ops.VAULT_NO_KEY_HOLDER


def test_read_set_key_carries_the_refusal_rather_than_a_null_payload(
    graph_db_env, vault_schema, vault
):
    """``read_set_key`` is ``read_set`` narrowed to one key and shares its
    semantics exactly — including this one. A row handed back with a null
    payload and nothing else is the absence a refusal must not become."""
    ops.add_setting(
        VAULT_SET, 1, "default", {"access_token": SECRET}, org=ops.CALLER_ORG,
    )
    assert settings_ops.read_set_key(VAULT_SET, "default", org=None)["payload"] == {
        "access_token": SECRET
    }
    assert "vault_error" not in settings_ops.read_set_key(
        VAULT_SET, "default", org=None
    )

    settings_ops.set_vault_key_holder(None)
    row = settings_ops.read_set_key(VAULT_SET, "default", org=None)
    assert row is not None, "the refused member vanished from the narrowed read"
    assert row["payload"] is None
    assert row["vault_error"]["reason"] == settings_ops.VAULT_NO_KEY_HOLDER


def test_a_row_of_a_vault_set_that_is_not_a_locator_is_refused(
    graph_db_env, vault_schema, vault
):
    """Whatever it holds is not what this set stores, and handing it back
    would present it as the value."""
    ops.add_setting(VAULT_SET, 1, "default", {"t": 1}, org=ops.CALLER_ORG)
    import sqlite3

    conn = sqlite3.connect(graph_db_env)
    with conn:
        conn.execute(
            "UPDATE settings SET payload = ? WHERE set_id = ?",
            (json.dumps({"access_token": "planted-in-the-clear"}), VAULT_SET),
        )
    conn.close()

    member = member_of(VAULT_SET)
    assert member.vault_error.reason == settings_ops.VAULT_NOT_A_LOCATOR
    assert member.payload is None


# ── the secured tier ──────────────────────────────────────────────────────


def test_a_secured_setting_resolves_to_a_sealed_key_and_not_to_a_value(
    graph_db_env, secured_schema, vault
):
    """Holding the storage state proves membership and yields exactly the
    content key, still sealed under the policy class. Applying the human
    factor is not resolution's job — and read_set holds no factor for
    anyone."""
    ops.add_setting(
        SECURED_SET, 1, "default", {"access_token": SECRET}, org=ops.CALLER_ORG,
    )
    member = member_of(SECURED_SET)

    assert member.payload is None
    assert member.vault_error is None
    sealed = member.sealed_content_key
    assert sealed["policy_class_id"] == vault.policy_class.class_id
    assert sealed["required_policy"] == vault.policy_class.policy
    assert SECRET not in json.dumps(member.to_dict())

    # It really is the content key, still sealed: the factor opens it to a
    # key, which is what makes this a nesting rather than a label.
    content_key = open_cek(
        vault.policy_class,
        vault.opener_seeds,
        sealed["sealed_cek"],
        genesis_id=vault.genesis_id,
        setting_name=object_id_for(vault.genesis_id, SECURED_SET, "default"),
        required_policy=vault.policy_class.policy,
    )
    assert isinstance(content_key, bytes) and len(content_key) == 32


def test_a_frozen_secured_setting_opens_to_plaintext_at_the_one_chokepoint(
    graph_db_env, secured_schema, vault
):
    """The approval-facing seam consumes a factor and returns the payload,
    while its frozen Setting digest prevents a later row from being substituted.
    """
    setting_id = ops.add_setting(
        SECURED_SET, 1, "default", {"access_token": SECRET},
        org=ops.CALLER_ORG,
    )
    with VaultStore(graph_db_env) as store:
        store.put_class(vault.policy_class)
    member = member_of(SECURED_SET)
    digest = hashlib.sha256(
        canonical_json(member.sealed_content_key)
    ).hexdigest()

    # B-1: the operator's browser opens the policy class locally and hands the
    # server only this one revision's CEK. Reproduce that with open_cek, then
    # drive the one server-side chokepoint with the resulting content key.
    content_key = open_cek(
        vault.policy_class,
        vault.opener_seeds,
        member.sealed_content_key["sealed_cek"],
        genesis_id=vault.genesis_id,
        setting_name=object_id_for(vault.genesis_id, SECURED_SET, "default"),
        required_policy=vault.policy_class.policy,
    )

    assert settings_ops.open_secured_setting(
        SECURED_SET,
        "default",
        setting_id=setting_id,
        sealed_content_key_digest=digest,
        content_key=content_key,
        org=None,
    ) == {"access_token": SECRET}

    settings_ops.override_setting(
        setting_id, {"access_token": "replacement"}, org=None,
    )
    with pytest.raises(VaultError, match="changed before approval"):
        settings_ops.open_secured_setting(
            SECURED_SET,
            "default",
            setting_id=setting_id,
            sealed_content_key_digest=digest,
            content_key=content_key,
            org=None,
        )


def test_a_secured_set_whose_row_names_the_audited_tier_is_refused(
    graph_db_env, secured_schema, vault_schema, vault
):
    """A stored downgrade. The set declares how its secrets are released;
    a row saying otherwise is not a per-row preference."""
    ops.add_setting(VAULT_SET, 1, "default", {"t": 1}, org=ops.CALLER_ORG)
    audited_locator = json.dumps(
        stored_payload(graph_db_env, member_of(VAULT_SET).id)
    )
    ops.add_setting(SECURED_SET, 1, "default", {"t": 2}, org=ops.CALLER_ORG)
    import sqlite3

    conn = sqlite3.connect(graph_db_env)
    with conn:
        conn.execute(
            "UPDATE settings SET payload = ? WHERE set_id = ?",
            (audited_locator, SECURED_SET),
        )
    conn.close()

    member = member_of(SECURED_SET)
    assert member.vault_error.reason == settings_ops.VAULT_TIER_MISMATCH
    assert member.payload is None
    assert member.sealed_content_key is None


# ── an ordinary set is untouched ──────────────────────────────────────────


def test_a_non_vault_set_resolves_exactly_as_it_did_before(
    graph_db_env, plain_schema, vault
):
    """Byte equality with the pre-change serialized shape — the same field
    set, in the same order, with no vault field present at all."""
    payload = {"a": 1, "nested": {"b": [1, 2, 3]}, "s": "plain value"}
    setting_id = ops.add_setting(
        PLAIN_SET, 1, "default", payload, org=ops.CALLER_ORG,
    )
    result = settings_ops.read_set(PLAIN_SET, org=None)
    body = result.as_payload()

    assert list(body["members"][0]) == [
        "id", "set_id", "stored_revision", "key", "payload", "state",
        "supersedes", "excludes", "deprecated", "successor_id", "created_at",
        "updated_at", "target_revision", "org", "upconverted",
    ]
    assert body["members"][0]["id"] == setting_id
    assert body["members"][0]["payload"] == payload
    assert json.dumps(body["members"][0]) == json.dumps(
        result.members[0].to_dict()
    )
    assert vault.holder_calls == [], "an ordinary set consulted the vault"


def test_an_ordinary_set_never_consults_the_key_holder(
    graph_db_env, plain_schema, vault
):
    """The pipeline is cheap because steps one to five never parse a payload
    and step six only exists for a set that declares itself vaulted."""
    for key in ("a", "b", "c"):
        ops.add_setting(PLAIN_SET, 1, key, {"k": key}, org=ops.CALLER_ORG)
    assert len(settings_ops.read_set(PLAIN_SET, org=None)) == 3
    assert vault.holder_calls == []


def test_the_key_holder_is_consulted_once_for_a_whole_set(
    graph_db_env, vault_schema, vault
):
    """Not once per member: one read of one organization's set asks for its
    key control once, however many secrets it resolves."""
    for key in ("a", "b", "c"):
        ops.add_setting(VAULT_SET, 1, key, {"k": key}, org=ops.CALLER_ORG)
    vault.holder_calls.clear()

    assert len(settings_ops.read_set(VAULT_SET, org=None)) == 3
    assert vault.holder_calls == [(VAULT_SET, None)]


# ── no second decryption path ─────────────────────────────────────────────


def test_settings_ops_derives_no_key_and_opens_no_bridge_itself():
    """The whole sequence already exists in ``objects.read_object`` — content
    address, secret recovery through bridges, descriptor commitment, unwrap,
    open. A parallel implementation here would be a second thing to get
    wrong, and the derivations bind their fields with ``canonical_json``, so
    a hand-written one that looks right produces a different key."""
    source = Path(settings_ops.__file__).read_text()
    for forbidden in ("WRAP_INFO_LABEL", "EDGE_INFO_LABEL", "HKDFExpand"):
        assert forbidden not in source, (
            f"{forbidden} in settings_ops.py — step six calls read_object, it "
            f"does not re-derive the construction"
        )
    assert "open_revision_for_member" in source
