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


def _grant_carries_checkpoint(store, grant_event_id) -> bool:
    from tools.network.ledger import membership_commitment as mc

    if not grant_event_id:
        return False
    try:
        grant = store.get(grant_event_id)
        return mc.CHECKPOINT_SCOPE in list(grant.payload.get("scope") or [])
    except Exception:
        return False


def _is_checkpointer(store, genesis: str) -> bool:
    from tools.graph import org_ops
    from tools.network.ledger import membership_commitment as mc

    try:
        persona = org_ops.persona_pub_for_org(genesis)
        return bool(persona) and persona in mc.checkpointer_pubs(store.fold())
    except Exception:
        return False


def prepare(org: str) -> dict:
    """Read signing inputs and cold-readable metadata; do not create a ledger."""
    path = org_ledger_db_path(org)
    if org in ("personal", "machine") or not path.exists():
        raise ValueError("organization delegate needs a founded organization")
    with LedgerStore(path) as store:
        genesis = store.ledger.genesis_id
        parents = list(store.heads())
        # The signing persona's delegate carries the checkpoint scope only
        # when that persona is a checkpointer now: the fold's bounded
        # self-delegation admits no scope the granter does not hold.
        checkpointer = _is_checkpointer(store, genesis)
        row = settings_ops.read_set_key(NETWORK_STORAGE_DELEGATE_SET_ID, genesis, org=None)
        metadata = dict((row or {}).get("payload") or {})
        # A checkpointer whose RECORDED grant predates the checkpoint scope
        # re-mints at this sign-on, so the admission step can publish (with
        # no grant recorded the browser mints anyway).
        remint_required = bool(
            checkpointer and metadata.get("grant_event_id")
            and not _grant_carries_checkpoint(store, metadata.get("grant_event_id")))
    # The reference is public; inspecting presence must not open the secret.
    if metadata:
        metadata["key_exists"] = settings_ops.chain_setting(
            VAULT_AUDITED_SET_ID, metadata["key_reference"], org=None,
        ) is not None
    return {
        "organization": org, "genesis_id": genesis, "parents": parents,
        "scope": storage_delegate_scopes(organization_content_domain_id(genesis),
                                         checkpointer=checkpointer),
        "checkpointer": checkpointer,
        "remint_required": remint_required,
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


def _genesis_of(org: str):
    path = org_ledger_db_path(org) if org else None
    if path is None or not path.exists():
        return None
    with LedgerStore(path) as store:
        return store.ledger.genesis_id


def signing_key(org: str):
    """Open the target organization's existing audited key on demand."""
    genesis = _genesis_of(org)
    if genesis is None:
        return None
    row = settings_ops.read_set_key(NETWORK_STORAGE_DELEGATE_SET_ID, genesis, org=None)
    metadata = (row or {}).get("payload") or {}
    # The index is keyed by this ledger's genesis. Its recorded slug is local
    # display context and may differ after rename or fleet replication.
    if not metadata:
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


#: org slug -> (SigningContext, delegate expires_at ms). Resolving a context
#: opens the ledger store and reads the personal index and the audited
#: vault: ~8 ms per open on SJC-2 (2026-09-29, 22 events), which S2 as
#: merged paid on EVERY organization write. Now paid once per delegate
#: lifetime; the write boundary re-judges the key against the fold cached
#: by ledger depth (settingskit.authority), so a revocation or a narrowed
#: role still refuses the very next write.
_SIGNING_CONTEXTS: dict = {}


def forget_signing_context(org: str | None = None) -> None:
    """Drop the cached signer for *org* (all when None): a renewed or
    replaced delegate is picked up on the next write."""
    if org is None:
        _SIGNING_CONTEXTS.clear()
    else:
        _SIGNING_CONTEXTS.pop(org, None)


def signing_context(org: str):
    """The settings signer for *org* (settings_ops.SigningContext), or None.

    The storage delegate's key signs the row; its member persona, resolved
    through the fold (a delegate key walks the delegation edges to the
    member it acts for), is the row's terminal persona. The witness cited is
    the attestation this node holds for the organization; none is held
    today (the adopted-checkpoint cache carries no attestation), so the
    envelope states None, and the boundary's witness bound (step 7) cannot
    apply until a node holds one (auto-qrmlg.6 S3 precondition).
    """
    from tools.graph.settings_ops import SigningContext

    now = int(time.time() * 1000)
    cached = _SIGNING_CONTEXTS.get(org)
    if cached is not None and cached[1] > now:
        return cached[0]
    _SIGNING_CONTEXTS.pop(org, None)
    key = signing_key(org)
    if key is None:
        return None
    genesis, persona = _resolve_signer_persona(org, key.public_hex, now)
    if persona is None:
        return None
    row = settings_ops.read_set_key(NETWORK_STORAGE_DELEGATE_SET_ID, genesis, org=None)
    expires_at = int(((row or {}).get("payload") or {}).get("expires_at") or 0)
    context = SigningContext(key=key, terminal_persona=persona, genesis_id=genesis, witness=None)
    if expires_at > now:
        _SIGNING_CONTEXTS[org] = (context, expires_at)
    return context


def _resolve_signer_persona(org: str, public_hex: str, now: int):
    """(genesis id, the member persona *public_hex* acts for or None), from
    the organization's ledger folded now. The one store open per resolution."""
    from tools.network.ledger import fold as fold_ledger
    from tools.network.settingskit.boundary import resolve_signer_persona

    with LedgerStore(org_ledger_db_path(org)) as store:
        genesis = store.ledger.genesis_id
        frontier = fold_ledger(store.ledger, now=now)
    return genesis, resolve_signer_persona(frontier, public_hex)


def install_settings_signer() -> None:
    """Make this process sign organization settings rows with its storage
    delegates (settings_ops.install_signer_provider)."""
    from tools.graph import settings_ops

    settings_ops.install_signer_provider(signing_context)


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
    # Either defined shape is a storage delegate: the two storage scopes, or
    # those plus the checkpoint scope for a checkpointer. Which one the
    # persona may mint is the FOLD's call below (bounded self-delegation
    # refuses a scope the granter does not hold), not a comparison against a
    # scope list computed at another moment: founding mints before the
    # founder's claim exists, and prepare() reads the fold after it.
    domain_id = organization_content_domain_id(context["genesis_id"])
    shapes = (storage_delegate_scopes(domain_id), storage_delegate_scopes(domain_id, checkpointer=True))
    if (event.type != "delegate" or p["child_pub"] != key.public_hex
            or sorted(p["scope"]) not in shapes or p["can_redelegate"]
            or p.get("ttl") != TTL_MS):
        raise ValueError("organization storage grant has incorrect key or terms")
    with LedgerStore(org_ledger_db_path(org)) as store:
        known = event.event_id in store.ledger
        current = context["delegate_metadata"].get("grant_event_id")
        if known and current in store.ledger and event.event_id in store.ledger.ancestry([current]):
            # This handoff completed, or a later grant has replaced it.
            # Replaying an acknowledged grant must not roll the pointer back.
            return
        if not known and tuple(event.parents) != tuple(store.heads()):
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
        # The signed grant is the existing content-addressed idempotency key.
        # Keep the previously indexed secret intact until this grant's index
        # is published, and reuse this same stored object on repeat delivery.
        reference = "storage-delegate." + context["genesis_id"] + "." + event.event_id
        existing = settings_ops.read_set_key(VAULT_AUDITED_SET_ID, reference, org=None)
        if existing:
            if (existing.get("payload") or {}).get("value") != key.private_hex:
                raise ValueError("organization delegate grant key cannot be opened or does not match")
        else:
            settings_ops.add_setting(VAULT_AUDITED_SET_ID, 1, reference,
                                    {"value": key.private_hex}, org=None)
        event_id = store.append(event)
    settings_ops.upsert_by_key(NETWORK_STORAGE_DELEGATE_SET_ID, 1, context["genesis_id"], {
        "organization": org, "persona_pub": event.author_key,
        "public_key": key.public_hex, "key_reference": reference,
        "expires_at": event.hlc.ts + TTL_MS, "grant_event_id": event_id,
    }, org=None)
