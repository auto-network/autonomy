"""Transport-neutral member-claim protocol service.

The dashboard HTTP adapter and the org:join ViewerChannel adapter call these
byte-identical functions. Expected protocol outcomes are discriminated
``{"status": ...}`` envelopes; malformed input and internal faults raise.
"""

from __future__ import annotations

import time

from tools.network.idkit import verify_signature
from tools.network.idkit.errors import IdkitError
from tools.network.ledger import (
    Event,
    INVITE_CLAIMED,
    INVITE_EXPIRED,
    INVITE_LIVE,
    LedgerStore,
    SignatureError,
    approval_signing_input,
    org_ledger_db_path,
)
from tools.network.ledger.fold import (
    R_APPROVAL_MISSING,
    claim_requirement_status,
)

_TERMINAL_PENDING_REASONS = frozenset({"claim-expired", "legacy-staging"})


def _require_hex(value: str, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(ch not in "0123456789abcdef" for ch in value)
    ):
        raise ValueError(f"{name} must be 64 lowercase hex chars")
    return value


def _require_approval(approval: dict) -> dict:
    if not isinstance(approval, dict) or set(approval) != {"key", "sig"}:
        raise ValueError("approval must be exactly {key, sig}")
    _require_hex(approval["key"], "approval key")
    signature = approval["sig"]
    if (
        not isinstance(signature, str)
        or len(signature) != 128
        or any(ch not in "0123456789abcdef" for ch in signature)
    ):
        raise ValueError("approval sig must be 128 lowercase hex chars")
    return approval


def _open(org: str) -> LedgerStore:
    if not isinstance(org, str) or not org:
        raise ValueError("org must be a non-empty local slug")
    path = org_ledger_db_path(org)
    if not path.exists():
        raise FileNotFoundError("organization ledger is not founded")
    return LedgerStore(path)


def _terminal_pending(readiness: dict) -> dict | None:
    reason = readiness.get("reason")
    if reason in _TERMINAL_PENDING_REASONS:
        return {"status": "rejected", "reason": reason}
    return None


def _event_matches_position(event: Event, position: dict | None) -> bool:
    return (
        isinstance(position, dict)
        and event.parents == tuple(position.get("parents", ()))
        and event.hlc.to_list() == position.get("hlc")
    )


def context(org: str, invite_ref: str) -> dict:
    """Minting context for one invitation."""
    _require_hex(invite_ref, "invite_ref")
    with _open(org) as store:
        try:
            invite = store.get(invite_ref)
        except KeyError:
            return {"status": "gone", "reason": "invite-not-found"}
        if invite.type != "invite":
            return {"status": "gone", "reason": "invite-not-found"}
        heads = store.heads()
        max_hlc = max(
            (store.get(head).hlc for head in heads),
            default=invite.hlc,
        )
        invite_state = store.fold(now=int(time.time() * 1000)).invites.get(invite_ref)
        if invite_state != INVITE_LIVE:
            if invite_state == INVITE_EXPIRED:
                reason = "invite-expired"
            elif invite_state == INVITE_CLAIMED:
                reason = "invite-already-claimed"
            else:
                reason = "invite-not-found"
            return {"status": "gone", "reason": reason}
        return {
            "status": "ok",
            "genesis_id": store.ledger.genesis_id,
            "heads": list(heads),
            "max_hlc": max_hlc.to_list(),
            "granted_role": invite.payload["granted_role"],
            "binding": "key" if "invite_pub" in invite.payload else "token",
            "invite_expiry": invite.payload["expiry"],
            # r7kk4: the sponsor persona public key, copied EXACTLY from the
            # live invite event's own payload — the ledger is the sole source.
            # Never accepted from the request or grant, and never used for
            # authority beyond the fold checks already performed above. It lets
            # the org resolve the inviter's presentation from its member
            # directory, and remains a secondary provenance detail downstream.
            "sponsor_pub": invite.payload["sponsor"],
        }


def submit(org: str, event_wire) -> dict:
    """Initial or final invitee-signed submit; the only claim append path."""
    event = Event.from_json(event_wire)
    if event.type != "member.claim":
        raise ValueError("event type must be member.claim")
    with _open(org) as store:
        claim_key = store.claim_key(
            event.payload["invite_ref"],
            event.payload["persona_pub"],
        )
        pending = store.get_pending_claim(claim_key)
        readiness = None
        pinned_finalize = False
        if pending is not None:
            stored_position = (
                {
                    "parents": pending["parents"],
                    "hlc": [pending["hlc_ts"], pending["hlc_count"]],
                }
                if pending["parents"] is not None
                and pending["hlc_count"] is not None
                else None
            )
            pinned_finalize = _event_matches_position(
                event,
                stored_position,
            )
            if pinned_finalize:
                readiness = store.evaluate_pending_claim(claim_key)
                terminal = _terminal_pending(readiness)
                if terminal is not None:
                    return terminal

        try:
            invite = store.get(event.payload["invite_ref"])
        except KeyError:
            return {"status": "rejected", "reason": "invite-not-in-ancestry"}
        if invite.type != "invite":
            return {"status": "rejected", "reason": "invite-not-in-ancestry"}

        # Primary TTL enforcement is the org node's online wall clock and
        # deliberately precedes the deterministic event-HLC trial fold for
        # every INITIAL submit. A FINALIZE is the one exact staged position:
        # its initial submit already passed this clock gate, and the pinned
        # causal position is the ledger-visible redemption record.
        if not pinned_finalize:
            if int(time.time() * 1000) > invite.payload["expiry"]:
                return {"status": "rejected", "reason": "invite-expired"}
            if event.parents != store.heads():
                return {"status": "rejected", "reason": "stale-heads"}

        reason = store.evaluate_claim(event)
        if reason is None:
            store.append(event)
            store.drop_pending_claim(claim_key)
            store.refresh_projections()
            try:
                from tools.dashboard import member_directory
                member_directory.project_claim(
                    org, event.payload["persona_pub"], event.payload.get("profile"),
                )
            except Exception:
                pass  # the admission stands; the directory row is presentation only
            return {
                "status": "admitted",
                "kem_credential": event.payload.get("kem_credential"),
            }
        if reason == R_APPROVAL_MISSING:
            # Replaying the exact staged position without enough approvals
            # must not reset the server-wall-clock staging TTL.
            if not pinned_finalize:
                store.stage_pending_claim(event)
                readiness = store.evaluate_pending_claim(claim_key)
            if readiness is None:
                raise RuntimeError("pending claim readiness was not computed")
            terminal = _terminal_pending(readiness)
            if terminal is not None:
                return terminal
            return {
                "status": "pending",
                "have": readiness["have"],
                "need": readiness["need"],
            }
        return {"status": "rejected", "reason": reason}


#: Ledger events per bootstrap page (~600 B each: ~2.5 MB a page, well under
#: the join channel's per-message backstop). The joiner asks again with
#: ``after`` while ``more`` is set; events() is id-sorted, so index pages
#: are deterministic, and the joiner's append_bundle orders by parents.
BOOTSTRAP_EVENT_PAGE = 4096


def bootstrap(org: str, invite_ref: str, persona_pub: str, after: int = 0) -> dict:
    """What an ADMITTED member needs to install the organization locally.

    Served over the org:join channel only once the fold shows *persona_pub*
    as a valid member: every ledger event as canonical wire (the joiner
    re-folds from genesis and verifies for itself), the org's registry
    binding (org uuid, root key, registry url — the joiner checks them
    against the genesis event before storing), and the org's presentation.
    A persona the fold does not admit gets ``pending``, never the ledger.
    """
    _require_hex(invite_ref, "invite_ref")
    _require_hex(persona_pub, "persona_pub")
    with _open(org) as store:
        state = store.fold(now=int(time.time() * 1000))
        member = state.members.get(persona_pub)
        if member is None or not state.valid.get(getattr(member, "claim_id", ""), True):
            return {"status": "pending"}
        after = max(0, int(after))
        page = store.events()[after:after + BOOTSTRAP_EVENT_PAGE]
        events = [event.to_json().decode("utf-8") for event in page]
        more = after + len(page) < len(store.ledger)
        genesis_id = store.ledger.genesis_id
    from tools.graph import settings_ops
    from tools.graph.schemas.network_identity import NETWORK_BINDING_SET_ID
    binding = None
    try:
        rows = settings_ops.read_owned_set(NETWORK_BINDING_SET_ID, org=org).members
        if rows and isinstance(rows[0].payload, dict):
            binding = dict(rows[0].payload)
    except Exception:
        binding = None  # served without a binding; the joiner refuses to install
    own_persona = _own_persona(genesis_id)
    reachability = _reachability_rows(org, own_persona)
    return {"status": "ok", "events": events, "more": more, "binding": binding,
            "member_profiles": _member_profiles(org, reachability, own_persona),
            "reachability_rows": reachability}


#: Bound on the rows a bootstrap reply seeds a joiner with. The reply is one
#: message over the org:join channel; a 5,000-member directory with photos
#: would be ~60 MB of JSON, and the fleet engine replicates the same rows to
#: the joiner on its first pull anyway. A joiner needs SOME peers to dial,
#: not every peer: the sponsor's own row first, then the machines this node
#: has most recently synced with (the scheduler's per-peer success stamps,
#: keyed by the same serving machine key the reachability row is).
BOOTSTRAP_ROW_CAP = 1024


def _own_persona(genesis_id: object) -> str | None:
    """The serving operator's persona in this org, so their own rows lead."""
    try:
        from tools.graph import org_ops

        return org_ops.persona_pub_for_org(genesis_id)
    except Exception:
        return None


def _peer_recency(path) -> dict[str, int]:
    """Serving machine pub -> newest successful pull from it, from the org
    scope's own fleet-sync peer state. Empty when nothing has synced."""
    try:
        from tools.network.fleet_sync_scheduler import SQLiteFleetSyncStore

        return dict(SQLiteFleetSyncStore(path).peer_last_success())
    except Exception:
        return {}


def _reachability_rows(org: str, own_persona: str | None = None,
                       cap: int = BOOTSTRAP_ROW_CAP) -> list:
    """The org's replicated reachability rows (auto-mldvv), so a joiner's
    first pull has somewhere to dial before its own rows have crossed.
    Bounded to *cap*: own rows first, then most recently synced with."""
    try:
        from tools.graph.db import _org_db_path
        from tools.network.fleet_org_reachability import read_rows

        path = _org_db_path(org)
        rows = [{"key": key, **payload} for key, payload in read_rows(path).items()
                if isinstance(payload, dict)]
    except Exception:
        return []
    if len(rows) <= cap:
        recency = {}
    else:
        recency = _peer_recency(path)

    def rank(row: dict) -> tuple:
        own = 0 if own_persona and row.get("persona_pub") == own_persona else 1
        synced = -int(recency.get(str(row.get("key")), 0) or 0)
        published = -int(row.get("updated_at") or 0)
        return (own, synced, published, str(row.get("key")))

    rows.sort(key=rank)
    return rows[:cap]


def _member_profiles(org: str, reachability: list, own_persona: str | None,
                     cap: int = BOOTSTRAP_ROW_CAP) -> list:
    """Directory rows for the joiner's first render: name and byline only
    (the photo replicates with the row on the first org pull; the listing
    shows initials until then), for the personas the reachability rows name
    plus the sponsor, then the rest of the directory up to *cap*."""
    from tools.dashboard import member_directory

    profiles = {row["persona_pub"]: row for row in member_directory.rows(org)}
    order: list[str] = []
    if own_persona and own_persona in profiles:
        order.append(own_persona)
    for row in reachability:
        persona = row.get("persona_pub")
        if isinstance(persona, str) and persona in profiles and persona not in order:
            order.append(persona)
    for persona in profiles:
        if persona not in order:
            order.append(persona)
    out = []
    for persona in order[:cap]:
        row = dict(profiles[persona])
        row.pop("avatar", None)
        out.append(row)
    return out


def status(org: str, invite_ref: str, persona_pub: str) -> dict:
    """Pending/admitted/absent status derived from staging plus the fold."""
    _require_hex(invite_ref, "invite_ref")
    _require_hex(persona_pub, "persona_pub")
    with _open(org) as store:
        claim_key = store.claim_key(invite_ref, persona_pub)
        pending = store.get_pending_claim(claim_key)
        if pending is not None:
            readiness = store.evaluate_pending_claim(claim_key)
            terminal = _terminal_pending(readiness)
            if terminal is not None:
                return terminal
            invite = store.get(invite_ref)
            return {
                "status": "pending",
                # Resume bootstrap. These are derived from the staged claim
                # and its ledger, not accepted from the client. A restart can
                # therefore finalize after wall expiry without weakening the
                # fresh-join context() TTL gate.
                "genesis_id": store.ledger.genesis_id,
                "granted_role": invite.payload["granted_role"],
                "have": readiness["have"],
                "need": readiness["need"],
                "approvals": pending["approvals"],
                # The verdict-computed, need-sized subset. Resume clients
                # finalize with exactly these keys rather than reimplementing
                # the fold's authority decision after a restart.
                "admitting": readiness["admitting"],
                "position": readiness["position"],
            }
        member = store.fold().members.get(persona_pub)
        if member is not None and member.invite_id == invite_ref:
            return {"status": "admitted"}
        return {"status": "absent"}


def countersign(
    org: str,
    invite_ref: str,
    persona_pub: str,
    approval: dict,
) -> dict:
    """Gate one countersignature through the fold's single authority core."""
    _require_hex(invite_ref, "invite_ref")
    _require_hex(persona_pub, "persona_pub")
    _require_approval(approval)
    with _open(org) as store:
        claim_key = store.claim_key(invite_ref, persona_pub)
        pending = store.get_pending_claim(claim_key)
        if pending is None:
            return {"status": "absent"}
        if pending["invite_ref"] != invite_ref or pending["persona_pub"] != persona_pub:
            return {"status": "absent"}
        readiness = store.evaluate_pending_claim(claim_key)
        terminal = _terminal_pending(readiness)
        if terminal is not None:
            return terminal

        # Verify before authority feedback. add_pending_approval re-verifies
        # at the persistence boundary, preserving the store's fail-closed
        # contract even though the service must gate before merge.
        try:
            verify_signature(
                approval["key"],
                approval["sig"],
                approval_signing_input("member.claim", pending["body"]),
            )
        except IdkitError:
            return {"status": "rejected", "reason": "bad-signature"}

        invite = store.get(invite_ref)
        role = invite.payload["granted_role"]
        authority = store.fold(now=pending["hlc_ts"])
        role_view = authority.role_defs[role]
        have, _need = claim_requirement_status(
            requires=role_view.claim_requires,
            key_bound="invite_pub" in invite.payload,
            approver_keys=[approval["key"]],
            threshold=role_view.approver_threshold,
            root=authority.root,
            sponsor=invite.payload["sponsor"],
            role=role,
            holds=authority.holds,
        )
        if have == 0:
            return {
                "status": "rejected",
                "reason": "approver-not-authorized",
            }

        try:
            store.add_pending_approval(claim_key, approval)
        except SignatureError:
            return {"status": "rejected", "reason": "bad-signature"}
        readiness = store.evaluate_pending_claim(claim_key)
        terminal = _terminal_pending(readiness)
        if terminal is not None:
            return terminal
        if readiness["ready"]:
            return {
                "status": "ready",
                "have": readiness["have"],
                "need": readiness["need"],
                "kem_credential": pending["body"].get("kem_credential"),
                "admitting": readiness["admitting"],
                "position": readiness["position"],
            }
        return {
            "status": "pending",
            "have": readiness["have"],
            "need": readiness["need"],
            "position": readiness["position"],
        }
