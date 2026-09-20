"""Test kit: provision a serving credential the way the design of record
stores it (graph://67d0aa5f-885 D4, D5): the private key as a machine-vault
row, the certificates as this machine's ``autonomy.machine.serve-cert`` row.

Usable only from tests. Publishes the operator's audited delegate recipient
into the personal store (what unlock does) so cold writes seal, and installs
the delegate's private half to warm the vault in the test process.
"""
from __future__ import annotations

from tools.graph import settings_ops
from tools.graph.schemas.machine_serve_cert import (
    MACHINE_SERVE_CERT_REVISION,
    MACHINE_SERVE_CERT_SET_ID,
    serving_key_vault_key,
)
from tools.graph.schemas.machine_vault import (
    MACHINE_VAULT_AUDITED_REVISION,
    MACHINE_VAULT_AUDITED_SET_ID,
)


def publish_recipient(monkeypatch) -> str:
    """Publish the audited delegate recipient into personal.db and route the
    vault's policy-class lookup there. Returns the delegate's private hex."""
    from tools.graph.db import GraphDB, _org_db_path
    from tools.vault import key_holder
    from tools.vault.personal_object import derive_delegate_audited_recipient
    from tools.vault.store import VaultStore

    personal = _org_db_path("personal")
    GraphDB(personal).close()
    GraphDB.close_all_pooled()
    monkeypatch.setattr(key_holder, "_scoped_db", lambda _set_id, _org: personal)
    private_hex, public_hex = derive_delegate_audited_recipient(bytes(range(32)))
    with VaultStore(personal) as store:
        store.put_delegate_audited_recipient(public_hex)
    settings_ops.set_vault_sealer(None)
    settings_ops.set_vault_key_holder(None)
    settings_ops.set_personal_delegate_audited_key(None)
    return private_hex


def warm(private_hex: str) -> None:
    settings_ops.set_personal_delegate_audited_key(private_hex)


def cold() -> None:
    settings_ops.set_personal_delegate_audited_key(None)


def store_key(org_uuid: str, private_hex: str, child_pub: str | None = None) -> str:
    """Seal the serving key for *org_uuid* into the machine vault (cold write).
    *child_pub* defaults to the key's own public half."""
    from tools.network.idkit import KeyPair

    child_pub = child_pub or KeyPair.from_private_hex(private_hex).public_hex
    vault_key = serving_key_vault_key(org_uuid, child_pub)
    settings_ops.write_by_key(
        MACHINE_VAULT_AUDITED_SET_ID, MACHINE_VAULT_AUDITED_REVISION,
        vault_key, {"value": private_hex}, org="machine",
    )
    return vault_key


def remove_key(org_uuid: str) -> None:
    for member in settings_ops.read_set(MACHINE_VAULT_AUDITED_SET_ID, org="machine"):
        if member.key.startswith(f"serving-key.{org_uuid}."):
            settings_ops.remove_setting(member.id, org="machine")


def store_row(org_uuid: str, *, cert, child_pub: str, not_after: int,
              dns01_cert=None, viewer_cert=None, persona_pub=None, root_pub=None) -> dict:
    """Write this machine's serve-cert row for *org_uuid* (certificates only)."""
    payload = {
        "cert": cert, "not_after": not_after, "child_pub": child_pub,
        "vault_key": serving_key_vault_key(org_uuid, child_pub),
    }
    if dns01_cert is not None:
        payload["dns01_cert"] = dns01_cert
    if viewer_cert is not None:
        payload["viewer_cert"] = viewer_cert
    if persona_pub is not None:
        payload["persona_pub"] = persona_pub
    if root_pub is not None:
        payload["root_pub"] = root_pub
    settings_ops.upsert_by_key(
        MACHINE_SERVE_CERT_SET_ID, MACHINE_SERVE_CERT_REVISION, org_uuid,
        payload, org="machine",
    )
    return payload


def machine_row(org_uuid: str) -> dict | None:
    for member in settings_ops.read_set(MACHINE_SERVE_CERT_SET_ID, org="machine"):
        if member.key == org_uuid:
            return member.payload
    return None


def vaulted_keys(org_uuid: str) -> dict:
    """``{vault_key: member}`` for every serving key of *org_uuid* in the vault."""
    return {
        member.key: member
        for member in settings_ops.read_set(MACHINE_VAULT_AUDITED_SET_ID, org="machine")
        if member.key.startswith(f"serving-key.{org_uuid}.")
    }
