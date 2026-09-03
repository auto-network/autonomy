"""Can a PERSONAL-homed vaulted set actually be written — and read back?

`autonomy.vault.audited` and `.secured` are declared `@home("personal")` and
`@vaulted(...)`. A personal secret takes the owner-at-rest path, never the org
storage-domain path: a personal SECURED CEK seals to its policy class (the human
factor opens it), and a personal AUDITED CEK seals COLD to the dedicated delegate
recipient (the warm delegate opens it unattended). Neither consults the org
sealer, which resolves a founded ledger and exists only for org-homed sets.

These tests drive the real `settings_ops` write/read path against the shipped
`autonomy.vault.audited` set, so the cold-audited behaviour is a test result
rather than an argument.
"""

from __future__ import annotations

import pytest

from tools.graph import settings_ops
from tools.graph.db import GraphDB
from tools.graph.schemas.vault_credential import (
    VAULT_AUDITED_SET_ID,
    VAULT_CREDENTIAL_REVISION,
)
from tools.vault import key_holder
from tools.vault.errors import VaultError
from tools.vault.personal_object import derive_delegate_audited_recipient
from tools.vault.store import VaultStore

import tools.graph.schemas  # noqa: F401 — registers the sets


@pytest.fixture(autouse=True)
def cold_vault(tmp_path, monkeypatch):
    """A cold vault over a fresh personal.db: no sealer, no key holder, no warm
    delegate. The vault store and the settings rows share the one file."""
    monkeypatch.setenv("AUTONOMY_DATA_ROOT", str(tmp_path))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_API", raising=False)
    db = tmp_path / "personal.db"
    monkeypatch.setattr(key_holder, "_scoped_db", lambda _set_id, _org: db)
    GraphDB(db).close()
    GraphDB.close_all_pooled()
    settings_ops.set_vault_sealer(None)
    settings_ops.set_vault_key_holder(None)
    settings_ops.set_personal_delegate_audited_key(None)
    yield db
    GraphDB.close_all_pooled()
    settings_ops.set_vault_sealer(None)
    settings_ops.set_vault_key_holder(None)
    settings_ops.set_personal_delegate_audited_key(None)


def test_the_set_is_declared_personal_and_vaulted():
    """The premise. If either declaration changes, the rest of this file is
    describing a set that no longer exists."""
    from tools.graph import schemas

    assert schemas.declared_home(VAULT_AUDITED_SET_ID) == "personal"
    assert schemas.declared_vault_tier(VAULT_AUDITED_SET_ID) == "audited"


def test_audited_write_refuses_by_name_before_the_delegate_is_published(cold_vault):
    """Without a published delegate recipient there is no cold recipient to seal
    to. The write refuses naming the missing provisioning — it does NOT silently
    fall to the org sealer, which is only for org-homed sets."""
    with pytest.raises(VaultError, match="delegate recipient"):
        settings_ops.add_setting(
            VAULT_AUDITED_SET_ID,
            VAULT_CREDENTIAL_REVISION,
            "github.token",
            {"value": "ghp_" + "a" * 36},
            org=None,
        )


def test_ehyoh_can_store_the_operators_github_token(cold_vault):
    """auto-ehyoh: the operator's GitHub token in their OWN store, written COLD and
    released unattended — the corrected audited path (no org sealer, no factor)."""
    db = cold_vault

    # The operator publishes the delegate recipient at unlock; here, directly.
    private_hex, public_hex = derive_delegate_audited_recipient(bytes(range(32)))
    with VaultStore(db) as store:
        store.put_delegate_audited_recipient(public_hex)

    # COLD write: the sealer and key holder are both None.
    assert settings_ops._vault_sealer is None
    assert settings_ops._vault_key_holder is None
    token = "ghp_" + "a" * 36
    setting_id = settings_ops.add_setting(
        VAULT_AUDITED_SET_ID,
        VAULT_CREDENTIAL_REVISION,
        "github.token",
        {"value": token},
        org=None,
    )
    assert isinstance(setting_id, str) and setting_id

    # Cold read fails closed — no warm delegate, no plaintext.
    member = _member(settings_ops.read_set(VAULT_AUDITED_SET_ID, org=None))
    assert member.payload is None
    assert member.vault_error is not None
    assert member.vault_error.reason == settings_ops.VAULT_NO_KEY_HOLDER

    # Warm read: the delegate private half releases the token unattended.
    settings_ops.set_personal_delegate_audited_key(private_hex)
    member = _member(settings_ops.read_set(VAULT_AUDITED_SET_ID, org=None))
    assert member.vault_error is None
    assert member.payload["value"] == token

    # The ciphertext lives inline in personal.db — no org content sidecar.
    assert not (db.parent / "content").exists()


def test_audited_replacement_opens_with_the_revision_that_created_it(cold_vault):
    """A resolved Setting keeps the base id but opens the winning row's locator.

    Audited locators bind their encryption to the physical row UUID. A
    replacement is an override row with a new UUID, so authenticating its
    locator as though the base row created it makes every update unreadable
    despite the correct delegate being warm.
    """
    db = cold_vault
    private_hex, public_hex = derive_delegate_audited_recipient(bytes(range(32)))
    with VaultStore(db) as store:
        store.put_delegate_audited_recipient(public_hex)

    settings_ops.write_by_key(
        VAULT_AUDITED_SET_ID,
        VAULT_CREDENTIAL_REVISION,
        "github.token",
        {"value": "first"},
        org=None,
    )
    replacement_id = settings_ops.write_by_key(
        VAULT_AUDITED_SET_ID,
        VAULT_CREDENTIAL_REVISION,
        "github.token",
        {"value": "replacement"},
        org=None,
    )

    settings_ops.set_personal_delegate_audited_key(private_hex)
    member = _member(settings_ops.read_set(VAULT_AUDITED_SET_ID, org=None))
    assert member.vault_error is None
    assert member.payload == {"value": "replacement"}
    assert member.id != replacement_id, "the public identity remains the base row"


def _live_override_rows(db):
    with GraphDB(db) as gdb:
        return gdb.conn.execute(
            "SELECT id, supersedes, deprecated, successor_id FROM settings "
            "WHERE set_id = ? AND key = ? "
            "ORDER BY created_at, rowid",
            (VAULT_AUDITED_SET_ID, "github.token"),
        ).fetchall()


def test_vault_reseal_collapses_the_override_fan_to_one_live(cold_vault):
    """auto-2j6s0: sequential vault re-seals leave exactly one live override.

    write_by_key opts a vault set into atomic write+deprecate, so each re-seal
    deprecates the prior override (successor-linked) instead of stacking another
    live layer the resolver must merge. The value still resolves to the newest.
    """
    db = cold_vault
    private_hex, public_hex = derive_delegate_audited_recipient(bytes(range(32)))
    with VaultStore(db) as store:
        store.put_delegate_audited_recipient(public_hex)

    for value in ("first", "second", "third", "fourth"):
        settings_ops.write_by_key(
            VAULT_AUDITED_SET_ID, VAULT_CREDENTIAL_REVISION,
            "github.token", {"value": value}, org=None,
        )

    rows = _live_override_rows(db)
    bases = [r for r in rows if r["supersedes"] is None]
    overrides = [r for r in rows if r["supersedes"] is not None]
    live_overrides = [r for r in overrides if not r["deprecated"]]
    deprecated = [r for r in overrides if r["deprecated"]]

    assert len(bases) == 1, "the base row is never deprecated"
    assert len(live_overrides) == 1, "exactly one override survives after N writes"
    assert len(deprecated) == 2, "the two earlier overrides are deprecated"
    # Each deprecated override points at its successor — an audit chain, not orphans.
    for row in deprecated:
        assert row["successor_id"] is not None

    settings_ops.set_personal_delegate_audited_key(private_hex)
    member = _member(settings_ops.read_set(VAULT_AUDITED_SET_ID, org=None))
    assert member.vault_error is None
    assert member.payload == {"value": "fourth"}


def test_default_override_stays_append_only(cold_vault):
    """The opt-out path is unchanged: override_setting without the flag never
    deprecates, so two direct overrides both stay live (the pre-existing shape)."""
    db = cold_vault
    _, public_hex = derive_delegate_audited_recipient(bytes(range(32)))
    with VaultStore(db) as store:
        store.put_delegate_audited_recipient(public_hex)

    base_id = settings_ops.add_setting(
        VAULT_AUDITED_SET_ID, VAULT_CREDENTIAL_REVISION,
        "github.token", {"value": "base"}, org=None,
    )
    settings_ops.override_setting(base_id, {"value": "o1"}, org=None)
    settings_ops.override_setting(base_id, {"value": "o2"}, org=None)

    overrides = [r for r in _live_override_rows(db) if r["supersedes"] is not None]
    live = [r for r in overrides if not r["deprecated"]]
    assert len(live) == 2, "default override_setting appends without deprecating"


def _member(resolved):
    return {s.key: s for s in resolved}["github.token"]


def test_sealed_settings_pepper_mints_once_and_never_rotates(cold_vault):
    """`ensure_pepper_minted` (the bring-up step-3 hook) establishes the shared
    sealed-settings pepper and NEVER rotates it: a second call is a no-op that
    leaves the value byte-for-byte unchanged, because a rotated pepper would
    orphan every sealed store's address.

    The assertions are on the END STATE, not on whether this call was the one
    that minted — so the contract holds regardless of any ambient pepper (the
    audited seal refusing before a delegate is published is covered by
    ``test_audited_write_refuses_by_name_before_the_delegate_is_published``)."""
    db = cold_vault
    from tools.graph.sealed_settings import PEPPER_KEY, ensure_pepper_minted

    private_hex, public_hex = derive_delegate_audited_recipient(bytes(range(32)))
    with VaultStore(db) as store:
        store.put_delegate_audited_recipient(public_hex)

    ensure_pepper_minted()
    assert ensure_pepper_minted() is False  # present -> never a second write

    # A well-formed 32-byte secret that releases unattended once the delegate
    # is warm, and is stable across a redundant ensure call.
    settings_ops.set_personal_delegate_audited_key(private_hex)

    def _pepper_value():
        member = {s.key: s for s in settings_ops.read_set(
            VAULT_AUDITED_SET_ID, org=None)}[PEPPER_KEY]
        assert member.vault_error is None
        return member.payload["value"]

    value = _pepper_value()
    assert len(bytes.fromhex(value)) == 32
    assert ensure_pepper_minted() is False
    assert _pepper_value() == value  # no rotation
