"""What the RUNNING dashboard worker holds, per organization — the answer a
diagnostic must give from inside this process, because it is not knowable
from any other.

The vault is warm only inside the worker that unlocked it: the audited
delegate key, the organization KEM keys and the generation keys live in
process globals (``unlock_routes._VAULT_CACHE``, ``settings_ops``) that a
fresh ``python -m`` sees as empty. Every tool that inspected the vault from
its own process concluded "cold" and reported it as fact; the operator then
explained, each time, that the vault is only warm in the right process and
that this is how it must be checked. ``GET /api/vault/status`` answered the
one-bit version of that (the audited delegate). This module answers the
per-organization version, from the same process, so ``fleet_doctor`` run on
the host or by ``docker exec`` reports what serving actually believes.

State names only: whether keys are held and how many, delegate status,
membership commitment, serve-cert status, the connector's own status reply.
No key material, no tokens, no certificates.
"""

from __future__ import annotations

import os
import time
from typing import Any


def _org_generation_state_ids(org: str) -> set[str] | None:
    """The generation state ids recorded in *org*'s key-control store, or
    None when the store cannot be read (a fresh org has none, honestly 0)."""
    try:
        from tools.network.storagekit.keycontrol import KeyControlStore
        from tools.vault.db_content_store import vault_db_path_for

        with KeyControlStore(vault_db_path_for(org)) as kc:
            return set(kc.states)
    except Exception:
        return None


def _genesis_for(org: str) -> str | None:
    try:
        from tools.network.ledger import LedgerStore, org_ledger_db_path

        path = org_ledger_db_path(org)
        if not path.exists():
            return None
        with LedgerStore(path) as store:
            return store.ledger.genesis_id or None
    except Exception:
        return None


def _connector_status(org: str) -> dict:
    """The connector's own status reply for *org*'s scope, or why not."""
    from tools.dashboard import link_serving_supervisor as sup

    try:
        reply = sup.control(org, "connector-status", {}, timeout=3.0)
    except sup.TunnelUnavailable as exc:
        return {"reachable": False, "detail": str(exc), "kind": exc.kind}
    except Exception as exc:  # noqa: BLE001 — a status surface never raises
        return {"reachable": False, "detail": f"{type(exc).__name__}: {exc}"}
    if not isinstance(reply, dict):
        return {"reachable": False, "detail": "malformed connector-status reply"}
    return {
        "reachable": True,
        "serving": reply.get("serving") is True,
        "boot_commit": reply.get("boot_commit"),
        "fleet_runtime_configured": reply.get("fleet_runtime_configured"),
        "active_streams": reply.get("active_streams"),
        "serving_slot": reply.get("serving_slot"),
        "accepted_caps": reply.get("accepted_caps"),
        "locked_refusals": reply.get("locked_refusals"),
        "direct_listener": reply.get("direct_listener"),
        "tunnel": reply.get("tunnel"),
    }


def _membership(org: str, genesis_id: str | None) -> dict:
    """Whether this node can produce the membership commitment the relay's
    v3 hello rider needs: the ledger folds, a persona exists for the org,
    and roots come out. ``capable`` is the one-word answer."""
    if not genesis_id:
        return {"capable": False, "detail": "no ledger genesis for this org"}
    try:
        from tools.graph import org_ops

        persona = org_ops.persona_pub_for_org(genesis_id)
    except Exception as exc:  # noqa: BLE001
        return {"capable": False, "detail": f"persona lookup failed: {exc}"}
    if not persona:
        return {"capable": False, "detail": "no persona for this org on this node"}
    try:
        from tools.dashboard import membership_plane

        commitment = membership_plane.commitment_for_org(org)
    except Exception as exc:  # noqa: BLE001
        return {"capable": False, "persona": True,
                "detail": f"commitment unavailable: {type(exc).__name__}: {exc}"}
    members = commitment.get("members") if isinstance(commitment, dict) else None
    return {
        "capable": True,
        "persona": True,
        "members": len(members) if isinstance(members, (list, tuple, set)) else None,
        "in_member_set": (persona in members) if isinstance(members, (list, tuple, set)) else None,
    }


def collect(*, now_ms: int | None = None) -> dict[str, Any]:
    """The live per-organization report. Never raises; every probe that
    fails says so in its own field."""
    from tools.dashboard import link_serving_supervisor as sup
    from tools.dashboard import org_storage_delegate, unlock_routes
    from tools.graph import org_ops, settings_ops

    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    cache = unlock_routes._VAULT_CACHE.get("cache")
    held_states = set(cache.secrets) if cache else set()
    org_kem = unlock_routes._VAULT_CACHE.get("organization_kem_keys") or {}
    try:
        warm = bool(settings_ops.personal_delegate_audited_is_warm())
    except Exception:
        warm = False
    delegates: dict[str, dict] = {}
    try:
        for row in org_storage_delegate.status(warm=warm, now_ms=now_ms).get("organizations", []):
            delegates[row["org"]] = row
    except Exception as exc:  # noqa: BLE001
        delegates = {"_error": {"detail": f"{type(exc).__name__}: {exc}"}}

    organizations = []
    for scope in sup._discover_startup_orgs():
        entry: dict[str, Any] = {"org": scope}
        genesis = None if scope == sup.PERSONAL_SCOPE else _genesis_for(scope)
        entry["genesis_id"] = genesis
        if scope != sup.PERSONAL_SCOPE:
            recorded = _org_generation_state_ids(scope)
            entry["generation_keys"] = {
                "recorded": None if recorded is None else len(recorded),
                "open_in_worker": None if recorded is None else len(recorded & held_states),
            }
            entry["organization_kem_key_held"] = bool(genesis and genesis in org_kem)
            entry["delegate"] = delegates.get(scope) or {"status": "missing"}
            entry["membership"] = _membership(scope, genesis)
        try:
            entry["serve_cert"] = sup.serve_cert_state(scope).get("status", "missing")
        except Exception as exc:  # noqa: BLE001
            entry["serve_cert"] = f"unreadable: {exc}"
        entry["connector"] = _connector_status(scope)
        organizations.append(entry)

    return {
        "pid": os.getpid(),
        "audited_delegate_warm": warm,
        "personal_generation_keys_open": len(held_states),
        "organizations": organizations,
    }
