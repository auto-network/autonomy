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
    settings_ops.set_vault_sealer(None)
    settings_ops.set_vault_key_holder(None)
    settings_ops.set_personal_delegate_audited_key(None)
    yield db
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


def _member(resolved):
    return {s.key: s for s in resolved}["github.token"]


def test_sealed_settings_pepper_mints_once_and_only_once(cold_vault):
    """`ensure_pepper_minted` (the bring-up step-3 hook) writes the shared
    sealed-settings pepper exactly once. Presence in any state short-circuits
    without a write: a re-mint would rotate the pepper and orphan every sealed
    store's address."""
    db = cold_vault
    from tools.graph.sealed_settings import PEPPER_KEY, ensure_pepper_minted

    private_hex, public_hex = derive_delegate_audited_recipient(bytes(range(32)))
    with VaultStore(db) as store:
        store.put_delegate_audited_recipient(public_hex)

    assert ensure_pepper_minted() is True
    assert ensure_pepper_minted() is False  # present -> untouched

    # The minted value releases unattended once the delegate is warm, and is
    # a well-formed 32-byte hex secret.
    settings_ops.set_personal_delegate_audited_key(private_hex)
    member = {s.key: s for s in settings_ops.read_set(
        VAULT_AUDITED_SET_ID, org=None)}[PEPPER_KEY]
    assert member.vault_error is None
    assert len(bytes.fromhex(member.payload["value"])) == 32


def test_sealed_settings_pepper_refuses_before_delegate_published(cold_vault):
    """Ordering is load-bearing: before the audited delegate recipient is
    published, the mint fails by name rather than writing anything."""
    from tools.graph.sealed_settings import ensure_pepper_minted

    with pytest.raises(VaultError, match="delegate recipient"):
        ensure_pepper_minted()
