"""Organization signing keys: public preparation and existing personal audited storage.

The browser mints the delegate. This module never derives persona keys, keeps
no private-key cache, and uses the existing organization ledger for authority.
"""
from __future__ import annotations

import time

from tools.graph import settings_ops
from tools.graph.schemas.network_identity import NETWORK_STORAGE_DELEGATE_SET_ID
from tools.graph.schemas.vault_credential import VAULT_AUDITED_SET_ID
from tools.network.idkit import KeyPair
from tools.network.ledger import Event, LedgerStore, org_ledger_db_path
from tools.network.ledger.projections import organization_content_domain_id
from tools.network.storagekit.delegate import storage_delegate_scopes

TTL_MS = 90 * 24 * 60 * 60 * 1000
REMINT_BELOW_MS = 30 * 24 * 60 * 60 * 1000


def prepare(org: str) -> dict:
    """Read signing inputs and cold-readable metadata; do not create a ledger."""
    path = org_ledger_db_path(org)
    if org in ("personal", "machine") or not path.exists():
        raise ValueError("organization delegate needs a founded organization")
    with LedgerStore(path) as store:
        genesis = store.ledger.genesis_id
        parents = list(store.heads())
    row = settings_ops.read_set_key(NETWORK_STORAGE_DELEGATE_SET_ID, genesis, org=None)
    metadata = dict((row or {}).get("payload") or {})
    # The reference is public; inspecting presence must not open the secret.
    if metadata:
        from tools.graph.db import GraphDB
        with GraphDB(org="personal") as db:
            metadata["key_exists"] = db.conn.execute(
                "SELECT 1 FROM settings WHERE set_id=? AND key=? LIMIT 1",
                (VAULT_AUDITED_SET_ID, metadata["key_reference"]),
            ).fetchone() is not None
    return {
        "organization": org, "genesis_id": genesis, "parents": parents,
        "scope": storage_delegate_scopes(organization_content_domain_id(genesis)),
        "ttl_ms": TTL_MS, "remint_below_ms": REMINT_BELOW_MS,
        "delegate_metadata": metadata,
    }


def status(*, warm: bool, now_ms: int) -> dict:
    """Signed-in profile status from public metadata; never open signing keys."""
    from tools.dashboard.signon_preparation import organization_plans

    organizations = []
    for entry, _ in organization_plans():
        org = entry["slug"]
        metadata = prepare(org)["delegate_metadata"]
        remaining = metadata.get("expires_at", 0) - now_ms
        if not metadata.get("key_exists"):
            state = "missing"
        elif remaining <= 0:
            state = "expired"
        elif remaining < REMINT_BELOW_MS:
            state = "renew"
        else:
            state = "ready"
        organizations.append({"org": org, "status": state,
                              "expires_at": metadata.get("expires_at"),
                              "days_remaining": max(0, remaining // 86400000)})
    problems = [row for row in organizations if row["status"] != "ready"]
    detail = "Personal vault is warm." if warm else "Personal vault is locked."
    labels = {"missing": "key missing", "expired": "key expired", "renew": "renewal due"}
    if organizations:
        detail += " " + "; ".join(
            f'{row["org"]}: {row["days_remaining"]} days remaining'
            if row["status"] == "ready" else f'{row["org"]}: {labels[row["status"]]}'
            for row in organizations) + "."
    if problems or not warm:
        detail += " Unlock with your root to recover or renew the keys."
    states = {row["status"] for row in problems}
    value = ("Locked" if not warm else "Missing" if "missing" in states else
             "Expired" if "expired" in states else "Renew" if problems else "Running")
    return {"needs": bool(problems) or not warm, "value": value,
            "detail": detail, "organizations": organizations}


def signing_key(org: str):
    """Open the target organization's existing audited key on demand."""
    path = org_ledger_db_path(org) if org else None
    if path is None or not path.exists():
        return None
    with LedgerStore(path) as store:
        genesis = store.ledger.genesis_id
    row = settings_ops.read_set_key(NETWORK_STORAGE_DELEGATE_SET_ID, genesis, org=None)
    metadata = (row or {}).get("payload") or {}
    if not metadata or metadata["organization"] != org:
        return None
    if metadata["expires_at"] <= int(time.time() * 1000):
        return None
    secret = settings_ops.read_set_key(VAULT_AUDITED_SET_ID, metadata["key_reference"], org=None)
    payload = (secret or {}).get("payload") or {}
    if not payload.get("value"):
        return None
    key = KeyPair.from_private_hex(payload["value"])
    if key.public_hex != metadata["public_key"]:
        raise ValueError("organization delegate does not match its public index")
    return key


def accept(item: dict) -> None:
    """After personal warm-up: reuse, or validate/store/append a browser grant."""
    org = item["organization"]
    context = prepare(org)
    if item["action"] == "reuse":
        if item["key_reference"] != context["delegate_metadata"].get("key_reference"):
            raise ValueError("organization delegate reference does not match")
        if signing_key(org) is None:
            raise ValueError("organization delegate cannot be opened")
        return
    if item["action"] != "new":
        raise ValueError("unknown organization delegate action")
    key = KeyPair.from_private_hex(item["private_key"])
    event = Event.from_json(item["event"])
    event.verify_sig()
    p = event.payload
    if (event.type != "delegate" or p["child_pub"] != key.public_hex
            or p["scope"] != context["scope"] or p["can_redelegate"]
            or p.get("ttl") != TTL_MS):
        raise ValueError("organization storage grant has incorrect key or terms")
    with LedgerStore(org_ledger_db_path(org)) as store:
        if tuple(event.parents) != tuple(store.heads()):
            raise ValueError("organization delegation must cite current heads")
        # Verify admission using the existing fold before persisting the secret.
        from tools.network.ledger import fold
        from tools.network.storagekit.acceptance import resolve_member_key
        from copy import deepcopy
        candidate = deepcopy(store.ledger)
        candidate.add(event)
        frontier = fold(candidate, now=int(time.time() * 1000))
        if resolve_member_key(frontier, key.public_hex) != event.author_key:
            raise ValueError("organization storage grant is not member-authorized")
        reference = "storage-delegate." + context["genesis_id"]
        existing = settings_ops.read_set_key(VAULT_AUDITED_SET_ID, reference, org=None)
        if existing:
            settings_ops.override_setting(existing["id"], {"value": key.private_hex}, org=None)
        else:
            settings_ops.add_setting(VAULT_AUDITED_SET_ID, 1, reference,
                                    {"value": key.private_hex}, org=None)
        event_id = store.append(event)
    settings_ops.upsert_by_key(NETWORK_STORAGE_DELEGATE_SET_ID, 1, context["genesis_id"], {
        "organization": org, "persona_pub": event.author_key,
        "public_key": key.public_hex, "key_reference": reference,
        "expires_at": event.hlc.ts + TTL_MS, "grant_event_id": event_id,
    }, org=None)
