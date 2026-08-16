"""The settings write path, when the set holds secrets.

The claim these tests hold to account is that after writing a vault secret,
the value is nowhere in the database file — not in the payload column, not in
any other column, not anywhere in the bytes on disk. So they read the real row
back out of the real SQLite file rather than inspecting what the sealer
returned, which is the assertion that actually covers the exposure.

The sealer is driven by the same storage stack the object tests use: a
throwaway org founded in memory, a real key generation, a real content store
on disk. No browser, no operator, no network (crib §21).
"""

from __future__ import annotations

import json

import pytest

from tools.graph import ops, schemas, settings_ops
from tools.graph.schemas.registry import SCHEMAS, UPCONVERTERS
from tools.network.storagekit.store import ContentStore
from tools.network.storagekit.tests.conftest import World
from tools.vault.storage_object import (
    Holdings,
    is_vault_locator,
    open_revision,
    parse_locator,
    seal_revision,
)

SECRET = "sk-live-must-not-reach-the-database"


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
def _clear_sealer():
    settings_ops.set_vault_sealer(None)
    try:
        yield
    finally:
        settings_ops.set_vault_sealer(None)


@pytest.fixture
def vault_schema():
    """A set whose payloads are secrets — the shape of the live exposure."""

    @schemas.vaulted("audited")
    class VaultedV1(schemas.SettingSchema):
        set_id = "autonomy.test.vaulted"
        schema_revision = 1

    schemas.register_schema("autonomy.test.vaulted", 1, VaultedV1)
    return VaultedV1


@pytest.fixture
def plain_schema():
    class PlainV1(schemas.SettingSchema):
        set_id = "autonomy.test.plain"
        schema_revision = 1

    schemas.register_schema("autonomy.test.plain", 1, PlainV1)
    return PlainV1


class Vault:
    """A registered sealer over a founded org, plus what it takes to read back.

    This is the shape a host process installs at startup: it holds the writing
    persona, the domain's key control and the content store, and settings
    knows none of it.
    """

    def __init__(self, tmp_path):
        self.world = World(member_count=2)
        self.author = self.world.member(0)
        self.head, _ = self.world.mint_initial_state(self.author)
        self.store = ContentStore(tmp_path / "content")
        self.calls: list = []

    def holdings(self, persona=None) -> Holdings:
        return Holdings(
            secrets=self.world.held(persona or self.author),
            descriptors=self.world.stores.kc.states,
            bridges=self.world.stores.kc.bridges,
        )

    def sealer(self, *, set_id, schema_revision, key, setting_id, payload, tier, org):
        self.calls.append((set_id, key, setting_id, tier, org))
        return seal_revision(
            author=self.author,
            frontier=self.world.fold(),
            set_id=set_id,
            key=key,
            setting_id=setting_id,
            payload=payload,
            holdings=self.holdings(),
            ancestry=self.world.ancestry,
            content_store=self.store,
            tier=tier,
        ).locator

    def close(self):
        self.store.close()


@pytest.fixture
def vault(tmp_path):
    live = Vault(tmp_path / "vault")
    settings_ops.set_vault_sealer(live.sealer)
    try:
        yield live
    finally:
        live.close()


def db_bytes(db_path) -> bytes:
    """Every byte of the database file, or nothing when a refused write never
    created one."""
    return db_path.read_bytes() if db_path.exists() else b""


def stored_row(db_path, setting_id: str) -> dict:
    """The row as it actually sits in the file, read outside the ops layer."""
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM settings WHERE id = ?", (setting_id,)
        ).fetchone()
        assert row is not None, f"no settings row {setting_id}"
        return dict(row)
    finally:
        conn.close()


# ── the secret is not in the row ──────────────────────────────────────────


def test_a_vault_secret_leaves_no_ciphertext_and_no_plaintext_in_its_row(
    graph_db_env, vault_schema, vault
):
    payload = {"access_token": SECRET, "refresh_token": "rt-also-secret"}
    setting_id = ops.add_setting(
        "autonomy.test.vaulted", 1, "default", payload, org=ops.CALLER_ORG
    )

    row = stored_row(graph_db_env, setting_id)
    stored = json.loads(row["payload"])
    assert is_vault_locator(stored)

    reference = parse_locator(stored)
    # A locator and nothing else: no key material, no ciphertext, no payload.
    assert set(reference) >= {"object_id", "revision_id", "storage_state_id"}
    for column, value in row.items():
        text = "" if value is None else str(value)
        assert SECRET not in text, f"the secret reached the {column!r} column"
        assert "rt-also-secret" not in text

    # And not anywhere else in the file either.
    assert SECRET.encode() not in db_bytes(graph_db_env)

    # The object it names holds the value, and only the writer's key opens it.
    header, body = vault.store.get_object(
        reference["object_id"], reference["revision_id"]
    )
    assert SECRET.encode() not in body
    assert open_revision(
        stored, holdings=vault.holdings(), content_store=vault.store
    ) == payload


def test_the_same_setting_written_twice_is_one_object_and_two_revisions(
    graph_db_env, vault_schema, vault
):
    """Changing a setting appends a superseding row — the same way an ordinary
    setting changes (design §9), and the reason a setting maps onto an object
    with no translation layer."""
    first_id = ops.add_setting(
        "autonomy.test.vaulted", 1, "default", {"access_token": "one"},
        org=ops.CALLER_ORG,
    )
    second_id = settings_ops.override_setting(
        first_id, {"access_token": "two"}, org=None
    )
    assert first_id != second_id

    first = parse_locator(json.loads(stored_row(graph_db_env, first_id)["payload"]))
    second = parse_locator(json.loads(stored_row(graph_db_env, second_id)["payload"]))
    assert first["object_id"] == second["object_id"]
    assert first["revision_id"] != second["revision_id"]

    held = vault.holdings()
    assert open_revision(
        json.loads(stored_row(graph_db_env, first_id)["payload"]),
        holdings=held, content_store=vault.store,
    ) == {"access_token": "one"}
    assert open_revision(
        json.loads(stored_row(graph_db_env, second_id)["payload"]),
        holdings=held, content_store=vault.store,
    ) == {"access_token": "two"}


def test_a_different_key_of_the_same_set_is_a_different_object(
    graph_db_env, vault_schema, vault
):
    one = ops.add_setting(
        "autonomy.test.vaulted", 1, "alpha", {"t": 1}, org=ops.CALLER_ORG
    )
    two = ops.add_setting(
        "autonomy.test.vaulted", 1, "beta", {"t": 2}, org=ops.CALLER_ORG
    )
    assert parse_locator(json.loads(stored_row(graph_db_env, one)["payload"]))[
        "object_id"
    ] != parse_locator(json.loads(stored_row(graph_db_env, two)["payload"]))[
        "object_id"
    ]


# ── failing closed ────────────────────────────────────────────────────────


def test_with_no_sealer_the_write_is_refused_rather_than_written_in_the_clear(
    graph_db_env, vault_schema
):
    settings_ops.set_vault_sealer(None)
    with pytest.raises(settings_ops.VaultSealerMissing):
        ops.add_setting(
            "autonomy.test.vaulted", 1, "default", {"access_token": SECRET},
            org=ops.CALLER_ORG,
        )
    assert SECRET.encode() not in db_bytes(graph_db_env)


def test_a_sealer_that_fails_leaves_no_row_at_all(graph_db_env, vault_schema):
    def broken(**kwargs):
        raise RuntimeError("the key generation is unreachable")

    settings_ops.set_vault_sealer(broken)
    with pytest.raises(RuntimeError):
        ops.add_setting(
            "autonomy.test.vaulted", 1, "default", {"access_token": SECRET},
            org=ops.CALLER_ORG,
        )
    assert settings_ops.read_set("autonomy.test.vaulted", org=None).to_dict() == {}
    assert SECRET.encode() not in db_bytes(graph_db_env)


def test_a_sealer_returning_something_other_than_a_locator_is_refused(
    graph_db_env, vault_schema
):
    settings_ops.set_vault_sealer(lambda **kwargs: kwargs["payload"])
    with pytest.raises(settings_ops.VaultSealerMissing):
        ops.add_setting(
            "autonomy.test.vaulted", 1, "default", {"access_token": SECRET},
            org=ops.CALLER_ORG,
        )
    assert SECRET.encode() not in db_bytes(graph_db_env)


def test_a_vault_row_is_never_rewritten_in_place(graph_db_env, vault_schema, vault):
    """Its revision is derived from the row id, and a committed revision
    admits only a byte-identical replay."""
    with pytest.raises(ValueError, match="vault set"):
        settings_ops.upsert_by_key(
            "autonomy.test.vaulted", 1, "default", {"access_token": SECRET},
            org=None,
        )
    assert SECRET.encode() not in db_bytes(graph_db_env)


def test_an_override_supersedes_without_touching_what_it_supersedes(
    graph_db_env, vault_schema, vault
):
    """The earlier revision stays exactly as written, and both still open —
    which is what "immutable object, assembled by the reader" means."""
    first_id = ops.add_setting(
        "autonomy.test.vaulted", 1, "default", {"access_token": "one"},
        org=ops.CALLER_ORG,
    )
    before = stored_row(graph_db_env, first_id)["payload"]
    second_id = settings_ops.override_setting(
        first_id, {"access_token": SECRET}, org=None
    )

    assert stored_row(graph_db_env, first_id)["payload"] == before
    assert stored_row(graph_db_env, second_id)["supersedes"] == first_id
    assert SECRET.encode() not in db_bytes(graph_db_env)

    held = vault.holdings()
    assert open_revision(
        json.loads(before), holdings=held, content_store=vault.store
    ) == {"access_token": "one"}
    assert open_revision(
        json.loads(stored_row(graph_db_env, second_id)["payload"]),
        holdings=held, content_store=vault.store,
    ) == {"access_token": SECRET}


def test_the_surviving_locator_after_a_merge_is_one_write_entire(
    graph_db_env, vault_schema, vault
):
    """Resolution merges rows with RFC 7386 before anything is decrypted; a
    scalar locator can only be replaced whole, never spliced."""
    first_id = ops.add_setting(
        "autonomy.test.vaulted", 1, "default", {"access_token": "one"},
        org=ops.CALLER_ORG,
    )
    second_id = settings_ops.override_setting(
        first_id, {"access_token": "two"}, org=None
    )
    base = json.loads(stored_row(graph_db_env, first_id)["payload"])
    override = json.loads(stored_row(graph_db_env, second_id)["payload"])

    merged = settings_ops.json_merge_patch(base, override)
    assert merged == override
    assert open_revision(
        merged, holdings=vault.holdings(), content_store=vault.store
    ) == {"access_token": "two"}


def test_the_payload_is_still_held_to_its_schema(graph_db_env, vault):
    """Encryption is what happens to a value, not a reason to stop checking it."""

    @schemas.vaulted("audited")
    class StrictVaultedV1(schemas.SettingSchema):
        set_id = "autonomy.test.vaulted-strict"
        schema_revision = 1

        @classmethod
        def validate(cls, payload):
            super().validate(payload)
            if "access_token" not in payload:
                raise schemas.SchemaValidationError("access_token required")

    schemas.register_schema("autonomy.test.vaulted-strict", 1, StrictVaultedV1)

    with pytest.raises(schemas.SchemaValidationError):
        ops.add_setting(
            "autonomy.test.vaulted-strict", 1, "default", {"wrong": 1},
            org=ops.CALLER_ORG,
        )
    assert vault.calls == [], "the sealer ran on a payload that never validated"


def test_a_one_row_per_key_set_appends_rather_than_rewriting(
    graph_db_env, vault
):
    """A set declared one-row-per-key normally collapses an override into a
    rewrite of the row. A vaulted row cannot be rewritten — its revision is
    already committed — so the amendment appends instead."""

    @schemas.vaulted("audited")
    @schemas.keyed_per_entity(key_strategy="secret_name")
    class KeyedVaultedV1(schemas.SettingSchema):
        set_id = "autonomy.test.vaulted-keyed"
        schema_revision = 1

    schemas.register_schema("autonomy.test.vaulted-keyed", 1, KeyedVaultedV1)

    first_id = ops.add_setting(
        "autonomy.test.vaulted-keyed", 1, "default", {"access_token": "one"},
        org=ops.CALLER_ORG,
    )
    second_id = settings_ops.override_setting(
        first_id, {"access_token": "two"}, org=None
    )
    assert second_id != first_id
    assert stored_row(graph_db_env, second_id)["supersedes"] == first_id
    assert open_revision(
        json.loads(stored_row(graph_db_env, first_id)["payload"]),
        holdings=vault.holdings(), content_store=vault.store,
    ) == {"access_token": "one"}


def test_a_reader_with_no_vault_gets_the_locator_and_never_the_value(
    graph_db_env, vault_schema, vault
):
    """Resolution up to the merge step is unchanged and metadata-only: it
    hands back what the row holds. Unwrapping is step six and belongs to the
    read path (auto-6364n); what matters here is that nothing on the way
    there produces the plaintext or hides the row."""
    ops.add_setting(
        "autonomy.test.vaulted", 1, "default", {"access_token": SECRET},
        org=ops.CALLER_ORG,
    )
    resolved = settings_ops.read_set("autonomy.test.vaulted", org=None).to_dict()
    assert is_vault_locator(resolved["default"].payload)
    assert SECRET not in json.dumps(resolved["default"].to_dict())


# ── ordinary settings are untouched ───────────────────────────────────────


def test_an_ordinary_setting_is_written_exactly_as_before(
    graph_db_env, plain_schema, vault
):
    """Byte equality with the pre-change behaviour: the payload column holds
    ``json.dumps(payload)``, which is the literal expression the write path
    used before this bead and still uses."""
    payload = {"a": 1, "nested": {"b": [1, 2, 3]}, "s": "plain value"}
    setting_id = ops.add_setting(
        "autonomy.test.plain", 1, "default", payload, org=ops.CALLER_ORG
    )

    row = stored_row(graph_db_env, setting_id)
    assert row["payload"] == json.dumps(payload)
    assert json.loads(row["payload"]) == payload
    assert not is_vault_locator(json.loads(row["payload"]))
    assert vault.calls == [], "an ordinary setting reached the vault"

    resolved = settings_ops.read_set("autonomy.test.plain", org=None).to_dict()
    assert resolved["default"].payload == payload


def test_an_ordinary_setting_is_written_with_no_vault_at_all(
    graph_db_env, plain_schema
):
    """Nothing about the ordinary path depends on a sealer existing."""
    settings_ops.set_vault_sealer(None)
    payload = {"a": 1}
    setting_id = ops.add_setting(
        "autonomy.test.plain", 1, "default", payload, org=ops.CALLER_ORG
    )
    assert stored_row(graph_db_env, setting_id)["payload"] == json.dumps(payload)


def test_an_ordinary_setting_still_upserts_and_overrides(graph_db_env, plain_schema):
    settings_ops.set_vault_sealer(None)
    base = ops.add_setting(
        "autonomy.test.plain", 1, "default", {"a": 1}, org=ops.CALLER_ORG
    )
    settings_ops.override_setting(base, {"a": 2}, org=None)
    assert settings_ops.read_set("autonomy.test.plain", org=None).to_dict()[
        "default"
    ].payload == {"a": 2}
