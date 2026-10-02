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
    # personal.db resolves via AUTONOMY_ORGS_DIR (it roots beside the orgs
    # tree), NOT AUTONOMY_DATA_ROOT. Another suite's conftest may have pinned
    # AUTONOMY_ORGS_DIR to a per-WORKER dir (dashboard hermetic stores), which
    # would put this test's settings rows in a db shared across the worker —
    # collisions and stale reads under xdist. Pin it per-test so personal.db is
    # tmp_path/personal.db, matching the vault store below.
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
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
    member = _member(settings_ops.read_set(VAULT_AUDITED_SET_ID, org=None, key_equals="github.token"))
    assert member.payload is None
    assert member.vault_error is not None
    assert member.vault_error.reason == settings_ops.VAULT_NO_KEY_HOLDER

    # Warm read: the delegate private half releases the token unattended.
    settings_ops.set_personal_delegate_audited_key(private_hex)
    member = _member(settings_ops.read_set(VAULT_AUDITED_SET_ID, org=None, key_equals="github.token"))
    assert member.vault_error is None
    assert member.payload["value"] == token

    # The ciphertext lives inline in personal.db — no org content sidecar.
    assert not (db.parent / "content").exists()


def test_audited_replacement_opens_with_the_revision_that_created_it(cold_vault):
    """A replacement is a new row (auto-z4582): audited locators bind their
    encryption to the physical row UUID, and the replacement row is the one
    that sealed it, so it opens under its own id."""
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
    member = _member(settings_ops.read_set(VAULT_AUDITED_SET_ID, org=None, key_equals="github.token"))
    assert member.vault_error is None
    assert member.payload == {"value": "replacement"}
    assert member.id == replacement_id, "the replacement IS the row now"


def test_org_writes_audited_cold_under_its_namespace_operator_reads(cold_vault):
    """auto-zp01m: an org seals audited under <org>:name in the operator's own
    store, cold to the delegate; the operator context reads it back warm. The
    org scoping is a namespace on a personal-home row, not a second database."""
    db = cold_vault
    private_hex, public_hex = derive_delegate_audited_recipient(bytes(range(32)))
    with VaultStore(db) as store:
        store.put_delegate_audited_recipient(public_hex)

    # An ORG (anchore) write — cold (no key holder), bearer-derived <org>:name.
    assert settings_ops._vault_key_holder is None
    sid = settings_ops.add_setting(
        VAULT_AUDITED_SET_ID, VAULT_CREDENTIAL_REVISION,
        "jira_token", {"value": "ghp_anchore"}, org="anchore",
    )
    assert isinstance(sid, str) and sid

    # It landed under the org namespace in the operator's OWN db.
    members = {m.key: m for m in settings_ops.read_set(VAULT_AUDITED_SET_ID, org=None)}
    assert "anchore:jira_token" in members
    assert "jira_token" not in members, "the bare name is not written; the org prefix is"
    # Cold read fails closed — no warm delegate, no plaintext.
    cold = settings_ops.read_set(VAULT_AUDITED_SET_ID, org=None, key_equals="anchore:jira_token")
    assert cold.members[0].vault_error is not None

    # Operator context (warm delegate) reads the org-namespaced value.
    settings_ops.set_personal_delegate_audited_key(private_hex)
    opened = {m.key: m for m in settings_ops.read_set(
        VAULT_AUDITED_SET_ID, org=None, key_equals="anchore:jira_token")}
    assert opened["anchore:jira_token"].vault_error is None
    assert opened["anchore:jira_token"].payload == {"value": "ghp_anchore"}


def test_audited_org_read_is_isolated_to_its_own_namespace(cold_vault):
    """SECURITY (auto-zp01m): an org session decrypts ONLY its <org>: audited
    rows — never another org's, never the operator's unprefixed rows. The scope
    filter is a pre-decrypt SQL WHERE, so an excluded row is never opened even
    with the delegate warm. Without the @org_writeback decorator this whole set
    was readable+decryptable by any session (cross-org plaintext exposure)."""
    db = cold_vault
    private_hex, public_hex = derive_delegate_audited_recipient(bytes(range(32)))
    with VaultStore(db) as store:
        store.put_delegate_audited_recipient(public_hex)

    settings_ops.add_setting(VAULT_AUDITED_SET_ID, VAULT_CREDENTIAL_REVISION,
                             "github.token", {"value": "OPERATOR"}, org=None)
    settings_ops.add_setting(VAULT_AUDITED_SET_ID, VAULT_CREDENTIAL_REVISION,
                             "jira_token", {"value": "ANCHORE"}, org="anchore")
    settings_ops.add_setting(VAULT_AUDITED_SET_ID, VAULT_CREDENTIAL_REVISION,
                             "jira_token", {"value": "AUTONOMY"}, org="autonomy")

    settings_ops.set_personal_delegate_audited_key(private_hex)  # vault warm

    auto = {m.key: m for m in settings_ops.read_set(VAULT_AUDITED_SET_ID, org="autonomy")}
    assert set(auto) == {"autonomy:jira_token"}, "an org sees only its own namespace"
    assert settings_ops.read_set(VAULT_AUDITED_SET_ID, org="autonomy", key_equals="autonomy:jira_token"
                                 ).members[0].payload == {"value": "AUTONOMY"}

    anchore = {m.key: m for m in settings_ops.read_set(VAULT_AUDITED_SET_ID, org="anchore")}
    assert set(anchore) == {"anchore:jira_token"}
    assert settings_ops.read_set(VAULT_AUDITED_SET_ID, org="anchore", key_equals="anchore:jira_token"
                                 ).members[0].payload == {"value": "ANCHORE"}

    operator = {m.key: m for m in settings_ops.read_set(VAULT_AUDITED_SET_ID, org=None)}
    assert {"github.token", "anchore:jira_token", "autonomy:jira_token"} <= set(operator)


def _live_override_rows(db):
    with GraphDB(db) as gdb:
        return gdb.conn.execute(
            "SELECT id, supersedes, deprecated, successor_id FROM settings "
            "WHERE set_id = ? AND key = ? "
            "ORDER BY created_at, rowid",
            (VAULT_AUDITED_SET_ID, "github.token"),
        ).fetchall()


def test_sequential_vault_reseals_leave_exactly_one_row(cold_vault):
    """auto-z4582: each re-seal replaces the row, so N writes leave one row,
    no overrides and nothing deprecated, and it resolves to the newest."""
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
    assert len(rows) == 1 and rows[0]["supersedes"] is None and not rows[0]["deprecated"]

    settings_ops.set_personal_delegate_audited_key(private_hex)
    member = _member(settings_ops.read_set(VAULT_AUDITED_SET_ID, org=None, key_equals="github.token"))
    assert member.vault_error is None
    assert member.payload == {"value": "fourth"}



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
            VAULT_AUDITED_SET_ID, org=None, key_equals=PEPPER_KEY)}[PEPPER_KEY]
        assert member.vault_error is None
        return member.payload["value"]

    value = _pepper_value()
    assert len(bytes.fromhex(value)) == 32
    assert ensure_pepper_minted() is False
    assert _pepper_value() == value  # no rotation
