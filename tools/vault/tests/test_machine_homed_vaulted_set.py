"""Can a MACHINE-homed audited set be written cold and read back warm, without
ever consulting the organization sealer?

Design of record graph://67d0aa5f-885 (2026-09-20): `autonomy.machine.vault.audited`
is `@home("machine")` and `@vaulted("audited")`. Its rows are secrets true on one
machine only (the fleet runtime credential, the serving delegate keys). It seals
exactly like a personal audited row — cold, to the operator's published audited
delegate recipient — and lands in the machine store, which never replicates.
Before the sealer branch this file drives, such a row fell through to the
organization sealer and could not be written at all.
"""

from __future__ import annotations

import json

import pytest

from tools.graph import settings_ops
from tools.graph.db import GraphDB, _org_db_path
from tools.graph.schemas.machine_vault import (
    MACHINE_VAULT_AUDITED_REVISION,
    MACHINE_VAULT_AUDITED_SET_ID,
    RUNTIME_CREDENTIAL_KEY,
)
from tools.vault import key_holder
from tools.vault.errors import VaultError
from tools.vault.personal_object import derive_delegate_audited_recipient
from tools.vault.store import VaultStore

import tools.graph.schemas  # noqa: F401 — registers the sets


@pytest.fixture(autouse=True)
def cold_vault(tmp_path, monkeypatch):
    """A cold vault: no sealer, no key holder, no warm delegate. personal.db
    holds the published delegate recipient; machine.db is where the rows go."""
    monkeypatch.setenv("AUTONOMY_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_API", raising=False)
    personal = tmp_path / "personal.db"
    # The policy-class set (where the recipient is published) is personal-homed,
    # so the sealer must look it up in personal.db whatever store the row targets.
    monkeypatch.setattr(key_holder, "_scoped_db", lambda _set_id, _org: personal)
    GraphDB(personal).close()
    GraphDB.close_all_pooled()
    settings_ops.set_vault_sealer(None)
    settings_ops.set_vault_key_holder(None)
    settings_ops.set_personal_delegate_audited_key(None)
    yield personal
    GraphDB.close_all_pooled()
    settings_ops.set_vault_sealer(None)
    settings_ops.set_vault_key_holder(None)
    settings_ops.set_personal_delegate_audited_key(None)


def _publish_recipient(personal_db):
    private_hex, public_hex = derive_delegate_audited_recipient(bytes(range(32)))
    with VaultStore(personal_db) as store:
        store.put_delegate_audited_recipient(public_hex)
    return private_hex


def _row(resolved, key):
    return {s.key: s for s in resolved}[key]


def test_the_set_is_declared_machine_homed_and_audited():
    from tools.graph import schemas

    assert schemas.declared_home(MACHINE_VAULT_AUDITED_SET_ID) == "machine"
    assert schemas.declared_vault_tier(MACHINE_VAULT_AUDITED_SET_ID) == "audited"


def test_a_machine_audited_write_refuses_by_name_before_the_recipient_is_published(cold_vault):
    """No published recipient, no cold recipient to seal to: refuse, naming the
    provisioning — never fall through to the organization sealer."""
    with pytest.raises(VaultError, match="delegate recipient"):
        settings_ops.add_setting(
            MACHINE_VAULT_AUDITED_SET_ID, MACHINE_VAULT_AUDITED_REVISION,
            RUNTIME_CREDENTIAL_KEY, {"value": "{}"}, org="machine",
        )


def test_the_runtime_credential_seals_cold_into_the_machine_store_and_opens_warm(cold_vault):
    personal = cold_vault
    private_hex = _publish_recipient(personal)
    credential = json.dumps({
        "machine_id": "ab" * 32, "machine_pub": "cd" * 32,
        "process_private_seed": "ef" * 32, "delegation_cert": {"v": 1},
        "serving_machine_private_seeds": {"uuid-anchore": "11" * 32},
        "org_sync_certs": {"anchore": {"child_pub": "a1" * 32}},
    }, sort_keys=True)

    # COLD write: sealer and key holder both None, delegate not warm.
    assert settings_ops._vault_sealer is None
    setting_id = settings_ops.add_setting(
        MACHINE_VAULT_AUDITED_SET_ID, MACHINE_VAULT_AUDITED_REVISION,
        RUNTIME_CREDENTIAL_KEY, {"value": credential}, org="machine",
    )
    assert isinstance(setting_id, str) and setting_id

    # The row is in machine.db, not personal.db: it must never replicate.
    machine_db = _org_db_path("machine")
    assert machine_db.exists()
    rows = GraphDB(machine_db).conn.execute(
        "SELECT count(*) FROM settings WHERE set_id=?", (MACHINE_VAULT_AUDITED_SET_ID,)
    ).fetchone()[0]
    GraphDB.close_all_pooled()
    assert rows == 1
    personal_rows = GraphDB(personal).conn.execute(
        "SELECT count(*) FROM settings WHERE set_id=?", (MACHINE_VAULT_AUDITED_SET_ID,)
    ).fetchone()[0]
    GraphDB.close_all_pooled()
    assert personal_rows == 0

    # Cold read fails closed: no warm delegate, no plaintext.
    member = _row(settings_ops.read_set(MACHINE_VAULT_AUDITED_SET_ID, org="machine"), RUNTIME_CREDENTIAL_KEY)
    assert member.payload is None
    assert member.vault_error is not None
    assert member.vault_error.reason == settings_ops.VAULT_NO_KEY_HOLDER

    # Warm read: the delegate's private half opens it unattended, whole.
    settings_ops.set_personal_delegate_audited_key(private_hex)
    member = _row(settings_ops.read_set(MACHINE_VAULT_AUDITED_SET_ID, org="machine"), RUNTIME_CREDENTIAL_KEY)
    assert member.vault_error is None
    assert json.loads(member.payload["value"])["serving_machine_private_seeds"] == {"uuid-anchore": "11" * 32}


def test_a_replacement_opens_with_the_row_that_created_it(cold_vault):
    """Every activation rewrites the credential; the newest one must open."""
    private_hex = _publish_recipient(cold_vault)
    settings_ops.write_by_key(
        MACHINE_VAULT_AUDITED_SET_ID, MACHINE_VAULT_AUDITED_REVISION,
        RUNTIME_CREDENTIAL_KEY, {"value": "first"}, org="machine",
    )
    settings_ops.write_by_key(
        MACHINE_VAULT_AUDITED_SET_ID, MACHINE_VAULT_AUDITED_REVISION,
        RUNTIME_CREDENTIAL_KEY, {"value": "second"}, org="machine",
    )
    settings_ops.set_personal_delegate_audited_key(private_hex)
    member = _row(settings_ops.read_set(MACHINE_VAULT_AUDITED_SET_ID, org="machine"), RUNTIME_CREDENTIAL_KEY)
    assert member.vault_error is None
    assert member.payload == {"value": "second"}


def test_an_organization_homed_vaulted_set_still_takes_the_organization_sealer(cold_vault):
    """The branch widened to the machine home must not have widened further:
    an org-homed audited set with no sealer registered is refused as before."""
    from tools.graph.schemas.network_identity import NETWORK_LINK_CHANNEL_KEY_SET_ID
    from tools.graph import schemas

    assert schemas.declared_home(NETWORK_LINK_CHANNEL_KEY_SET_ID) == "organization"
    _publish_recipient(cold_vault)
    with pytest.raises(settings_ops.VaultSealerMissing):
        settings_ops._seal_vault_payload(
            set_id=NETWORK_LINK_CHANNEL_KEY_SET_ID, schema_revision=1, key="tok",
            setting_id="00000000-0000-0000-0000-000000000001",
            payload={"private_key": "x"}, tier="audited", org="anchore",
        )
