"""The membership half of org-scope sync (design graph://c2baad48-0a3,
comment 30969266-a55): what this machine hands ``OrgFleetAuthenticator``
for every organization it is a member of, plus first contact.

Per organization, admission on the org hello needs three local facts and
one credential:

* the persona this node holds in the org (``autonomy.network.persona``);
* the newest membership checkpoint this node has ADOPTED
  (``membership_checkpoint._cached_adopted``: seq, members_root,
  ledger_head), and the member set behind it (a re-fold at that head);
* this persona's inclusion proof under that checkpoint;
* a persona certificate over this machine's per-organization SERVING
  machine key (auto-e2ufw: derived from the root, the genesis and the
  machine id, unlinkable across organizations, the relay's name for the
  machine) with scope ``fleet:sync`` and ``org`` = the genesis id, minted in
  the operator's browser at sign-on and delivered alongside the fleet
  runtime credential as ``org_sync_certs`` {slug: cert}. The personal
  fleet's process key is never used on the org path.

Everything here reads fresh from the stores each time it is asked, so a
checkpoint adopted after activation is honoured on the next hello. The
authenticators themselves are cached per process because they hold admitted
peers and replay state.

First contact: org discovery is the organization's replicated reachability
rows for direct addresses, and the org's LIVE serving slots at its relay
(``relay_slots_provider``) for everything else -- the same relay fallback
the personal fleet uses, reached through this machine's own connector for
that org. Nothing is stored for it.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Mapping


logger = logging.getLogger(__name__)

_lock = threading.Lock()
#: slug -> DelegationCert dict, as delivered with the runtime credential.
_certs: dict[str, dict] = {}
#: slug -> this machine's per-organization SERVING machine key for that org
#: (auto-e2ufw): the key the certificate names, the org hello's signer, the
#: reachability row's key, and the relay's name for this machine.
_keys: dict[str, Any] = {}
#: slug -> OrgFleetAuthenticator built for the installed key.
_channels: dict[str, Any] = {}


def _org_slugs() -> list[str]:
    from tools.graph import org_ops

    try:
        # A followed mirror (orgs.type='followed') is a read-only cache of
        # another org's public surface (design of record graph://5f2f5a49-00d
        # §10.4): this node holds no persona in it and mints no fleet:sync
        # certificate for it, so it is not an org-sync target. Skipping it here
        # also skips it in sync_org_targets, which iterates this list.
        return [ref.slug for ref in org_ops.list_orgs()
                if ref.slug not in ("personal", "machine")
                and ref.type != "followed"]
    except Exception:
        return []


def _genesis_id(slug: str) -> str | None:
    from tools.network.ledger import LedgerStore, org_ledger_db_path

    try:
        with LedgerStore(org_ledger_db_path(slug)) as store:
            return store.ledger.genesis_id
    except Exception:
        return None


def _org_uuid(slug: str) -> str | None:
    from tools.dashboard.network_routes import _first_member
    from tools.graph.schemas.network_identity import NETWORK_BINDING_SET_ID

    try:
        member = _first_member(NETWORK_BINDING_SET_ID, slug)
        value = (member.payload or {}).get("org_uuid") if member is not None else None
        return value if isinstance(value, str) and value else None
    except Exception:
        return None


def sync_org_targets() -> list[dict]:
    """The organizations the browser should mint a ``fleet:sync`` persona
    certificate for at sign-on: every local org with a genesis this node
    holds a persona in and a registry binding (its serving machine seed is
    delivered keyed by that org uuid). Every failure is a skip, never a
    raise."""
    from tools.graph import org_ops

    targets = []
    for slug in _org_slugs():
        genesis_id = _genesis_id(slug)
        if not genesis_id:
            continue
        try:
            persona_pub = org_ops.persona_pub_for_org(genesis_id)
        except Exception:
            persona_pub = None
        org_uuid = _org_uuid(slug)
        if not persona_pub or not org_uuid:
            continue
        targets.append({"scope": slug, "genesis_id": genesis_id,
                        "persona_pub": persona_pub, "org_uuid": org_uuid})
    return targets


def install(certs: object, keys: Mapping[str, Any]) -> int:
    """Keep the browser-minted certificates and this machine's per-org
    serving keys for this process, by org slug. A slug needs both to get a
    channel. Returns how many certificates were kept."""
    kept: dict[str, dict] = {}
    if isinstance(certs, dict):
        for slug, cert in certs.items():
            if isinstance(slug, str) and slug and isinstance(cert, dict) and cert.get("child_pub"):
                kept[slug] = dict(cert)
    with _lock:
        _certs.clear()
        _certs.update(kept)
        _keys.clear()
        _keys.update({k: v for k, v in dict(keys).items() if v is not None})
        _channels.clear()
    # The organization rosters exist from here (auto-qrmlg.8): write this
    # machine's live sessions into each org sink now rather than at the
    # next roster event.
    try:
        from tools.dashboard import session_presence

        session_presence.wake()
    except Exception:  # noqa: BLE001 — best-effort nudge
        pass
    return len(kept)


def keys_for_serving_seeds(serving_seeds: Mapping[str, str]) -> dict[str, Any]:
    """slug -> KeyPair from the sign-on's ``serving_machine_private_seeds``
    (keyed by registry org uuid), for the dashboard process."""
    from tools.network.idkit import KeyPair

    by_uuid = {t["org_uuid"]: t["scope"] for t in sync_org_targets()}
    out: dict[str, Any] = {}
    for org_uuid, seed_hex in dict(serving_seeds or {}).items():
        slug = by_uuid.get(org_uuid)
        if slug is None or not isinstance(seed_hex, str):
            continue
        try:
            out[slug] = KeyPair.from_private_hex(seed_hex)
        except Exception:
            continue
    return out


def keys_for_connector(certs: object, serving_machine_key) -> dict[str, Any]:
    """The one slug an org connector serves, matched by the certificate that
    names its serving machine key."""
    if serving_machine_key is None or not isinstance(certs, dict):
        return {}
    return {
        slug: serving_machine_key for slug, cert in certs.items()
        if isinstance(cert, dict) and cert.get("child_pub") == serving_machine_key.public_hex
    }


# -- membership callables (all local, no network) ------------------------------


def _adopted(slug: str) -> dict | None:
    from tools.dashboard import membership_checkpoint as cp

    record = cp._cached_adopted(slug)
    return record if isinstance(record, dict) and "seq" in record else None


def _history(slug: str) -> dict[int, dict]:
    from tools.dashboard import membership_checkpoint as cp

    return {seq: rec for seq, rec in cp.adopted_history(slug).items() if "seq" in rec}


def _members_at(slug: str, record: dict) -> tuple[str, ...]:
    from tools.dashboard import membership_checkpoint as cp
    from tools.network.ledger import membership_commitment as mc

    head = record.get("ledger_head")
    state = cp._fold_at(slug, [head]) if cp._is_head(head) else cp._fold_state(slug)[0]
    return tuple(mc.member_pubs(state))


#: Event types that can change the member set; a fold at any other event
#: as head reproduces the root of the nearest such ancestor. The same set
#: triggers a checkpoint at admission, so every checkpointed root is a
#: candidate here.
from tools.network.ledger.membership_commitment import MEMBER_SET_EVENT_TYPES as _LEAF_CHANGING

#: (slug, persona, heads) -> own-fold candidates. The list changes only
#: when events arrive (the heads change), so a hello never re-folds.
_own_fold_cache: dict[tuple[str, str, tuple[str, ...]], list] = {}


def _own_fold_roots(slug: str, persona_pub: str) -> list[tuple[str, tuple[str, ...]]]:
    """(members_root, members) of this node's OWN fold at heads it holds,
    newest first and distinct by root: the current fold, then the fold at
    each LEAF-CHANGING event as a head in reverse HLC order, back to and
    including this persona's admission claim (OrgAdmissionBundleBound.tla
    ProveOwnFold / Rotate, master ac114f59: a prover with no usable retained
    record still reaches a checkpointed root, since every member-set event
    is checkpointed and the claim's head is the floor). Other events cannot
    change the root (membership_commitment.MEMBER_SET_EVENT_TYPES), so they
    are not folded. Only folds whose set includes this persona are
    candidates. Memoized per head set."""
    from tools.network.ledger import membership_commitment as mc
    from tools.network.ledger.store import LedgerStore, org_ledger_db_path

    store = LedgerStore(org_ledger_db_path(slug))
    try:
        heads = tuple(store.heads())
        key = (slug, persona_pub, heads)
        cached = _own_fold_cache.get(key)
        if cached is not None:
            return list(cached)
        out: list[tuple[str, tuple[str, ...]]] = []
        seen: set[str] = set()
        current = store.fold()
        claim_id = None
        for member in current.members.values():
            if member.current_key == persona_pub:
                claim_id = str(member.claim_id)
        events = sorted(
            (e for e in store.events() if e.type in _LEAF_CHANGING),
            key=lambda e: (e.hlc.ts, e.hlc.count), reverse=True,
        )
        head_sets: list[list[str]] = [list(heads)] + [[e.event_id] for e in events]
        for index, hs in enumerate(head_sets):
            try:
                state = current if index == 0 else store.fold(heads=hs)
                members = tuple(mc.member_pubs(state))
            except Exception:
                continue
            root = mc.compute_root(members)
            if root not in seen and persona_pub in members:
                seen.add(root)
                out.append((root, members))
            if claim_id is not None and hs and hs[0] == claim_id:
                break
        if len(_own_fold_cache) > 256:
            _own_fold_cache.clear()
        _own_fold_cache[key] = list(out)
        return out
    finally:
        store.close()


def _claim_id_at(slug: str, record: dict | None, persona_pub: str) -> str | None:
    """The claim event that admits the member whose CURRENT key is
    *persona_pub* in the fold at *record*'s ledger_head (None: not a member
    there, or that head is not held). The newest fold (record None) gives
    the persona's current admission; a persona re-admitted after a removal
    carries a new claim id, and a rekey keeps its claim id."""
    from tools.dashboard import membership_checkpoint as cp

    try:
        if record is None:
            state = cp._fold_state(slug)[0]
        else:
            head = record.get("ledger_head")
            state = cp._fold_at(slug, [head]) if cp._is_head(head) else cp._fold_state(slug)[0]
    except Exception:
        return None
    for member in state.members.values():
        if member.current_key == persona_pub:
            return str(member.claim_id)
    return None


def _callables(slug: str, persona_pub: str) -> dict[str, Callable]:
    from tools.network.ledger import membership_commitment as mc

    def newest_adopted_seq():
        history = _history(slug)
        return max(history) if history else None

    def adopted_checkpoint_for(seq):
        return _history(slug).get(int(seq))

    def adopted_members_for(seq):
        record = adopted_checkpoint_for(seq)
        return _members_at(slug, record) if record is not None else None

    def retained_checkpoints():
        return list(_history(slug).values())

    member_sets: dict[tuple[int, str], tuple[str, ...]] = {}

    def members_for(record):
        key = (int(record["seq"]), str(record.get("ledger_head")))
        if key not in member_sets:
            member_sets[key] = _members_at(slug, record)
        return member_sets[key]

    def rider_under(label, members):
        index, path = mc.inclusion_proof(members, persona_pub)
        return {"v": 1, "checkpoint_seq": int(label), "index": index, "path": path}

    def membership_proof_for(under=None, root=None, attempt=0):
        """This machine's rider.

        *root* given (the server proving back at a client's level): under the
        retained record whose root that is, labelled *under*.

        Else the *attempt*-th candidate root, recomputed per call and
        rotated modulo the list (OrgAdmissionBundleBound.tla Rotate, master
        ac114f59): retained records whose head is held and whose set
        includes this persona, newest first; then this node's own fold at
        heads it holds, newest first, back to its admission claim. The
        label of an own-fold candidate is the newest retained seq, or 0; the
        verifier matches by root and reads the label as a hint only.
        Never raises: with nothing to prove under, the rider is empty and
        the peer refuses it."""
        history = _history(slug)
        if root is not None:
            for record in history.values():
                if record.get("members_root") != root:
                    continue
                try:
                    return rider_under(under if under is not None else record["seq"], members_for(record))
                except Exception:
                    break
        candidates: list[tuple[int, tuple[str, ...]]] = []
        for record in (history[k] for k in sorted(history, reverse=True)):
            try:
                members = members_for(record)
            except Exception:
                continue  # head not held yet
            if persona_pub in members:
                candidates.append((int(record["seq"]), members))
        newest_label = max(history) if history else 0
        retained_roots = {mc.compute_root(m) for _s, m in candidates}
        try:
            own = _own_fold_roots(slug, persona_pub)
        except Exception:
            own = []
        candidates += [(newest_label, m) for r, m in own if r not in retained_roots]
        if not candidates:
            return {"v": 1, "checkpoint_seq": int(newest_label), "index": 0, "path": []}
        label, members = candidates[int(attempt) % len(candidates)]
        return rider_under(label, members)

    def admission_ok_for(seq, peer_persona):
        """E-any-adm's floor: the record *seq* is at or after *peer_persona*'s
        current admission, i.e. the persona's claim in the fold at that
        record's head is the same claim that admits it in the newest fold.
        None when this node cannot tell (head not held, no record)."""
        record = _history(slug).get(int(seq))
        if record is None:
            return None
        at_record = _claim_id_at(slug, record, peer_persona)
        if at_record is None:
            return False
        current = _claim_id_at(slug, None, peer_persona)
        if current is None:
            return False
        return at_record == current

    return {
        "newest_adopted_seq": newest_adopted_seq,
        "adopted_checkpoint_for": adopted_checkpoint_for,
        "adopted_members_for": adopted_members_for,
        "retained_checkpoints": retained_checkpoints,
        "membership_proof_for": membership_proof_for,
        "admission_ok_for": admission_ok_for,
    }


def _build(slug: str, cert_dict: dict, machine_key, advertised) -> Any | None:
    from tools.network.fleet_org_channel import OrgFleetAuthenticator
    from tools.network.idkit import DelegationCert

    try:
        cert = DelegationCert.from_dict(cert_dict)
    except Exception:
        logger.warning("org sync: certificate for %s does not parse", slug)
        return None
    if cert.child_pub != machine_key.public_hex:
        logger.info("org sync: certificate for %s names another serving key; skipped", slug)
        return None
    if cert.subject.kind != "persona":
        return None
    callables = _callables(slug, str(cert.subject.id))
    try:
        return OrgFleetAuthenticator(
            machine_key, org=str(cert.org), persona_cert=cert,
            advertised_addresses=advertised, **callables,
        )
    except Exception:
        logger.warning("org sync: authenticator for %s could not be built", slug, exc_info=True)
        return None


def report() -> dict[str, dict]:
    """What this process holds per org slug, for the status surfaces
    (auto-mmwgu observability): the fleet:sync certificate's child key, the
    persona it names and its expiry, whether the matching serving key is
    held, and whether the channel has been built. A slug absent here has
    no certificate installed in this process, so its persona cut can never
    seal here."""
    with _lock:
        slugs = set(_certs) | set(_keys)
        out: dict[str, dict] = {}
        for slug in sorted(slugs):
            cert = _certs.get(slug)
            entry: dict = {
                "certificate": None,
                "key_held": slug in _keys,
                "channel": slug in _channels,
            }
            channel = _channels.get(slug)
            if channel is not None:
                try:
                    entry["hello"] = channel.hello_state()
                except Exception:  # noqa: BLE001
                    entry["hello"] = None
            if isinstance(cert, dict):
                subject = cert.get("subject") if isinstance(cert.get("subject"), dict) else {}
                entry["certificate"] = {
                    "child_pub": cert.get("child_pub"),
                    "persona": subject.get("id"),
                    "org": cert.get("org"),
                    "not_after": cert.get("not_after"),
                }
            out[slug] = entry
        return out


def provider(advertised_addresses: Callable[[], Any] | None = None):
    """The scheduler's ``org_channels`` provider: slug -> authenticator for
    every org with an installed certificate over an installed key. Built
    once per install and reused across rounds (admitted peers live there)."""

    def resolve() -> dict[str, Any]:
        with _lock:
            for slug, cert in _certs.items():
                key = _keys.get(slug)
                if slug in _channels or key is None:
                    continue
                channel = _build(slug, cert, key, advertised_addresses)
                if channel is not None:
                    _channels[slug] = channel
            return dict(_channels)

    return resolve


# -- relay slots (first contact through the org's own relay) ------------------

_SLOTS_TTL_S = 15.0
_slots_cache: dict[str, tuple[float, list]] = {}


def relay_slots_provider() -> Callable[[], Mapping[str, list]]:
    """The scheduler's ``org_relay_slots`` provider: for every org this
    machine holds a sync certificate for, the org's live serving slots at its
    relay, read through this machine's own connector for that org (its
    tunnel is on that relay). Cached briefly; a connector that is not up
    yields no slots for that org rather than an error."""
    import time as _time
    from tools.dashboard import link_serving_supervisor

    def resolve() -> dict[str, list]:
        out: dict[str, list] = {}
        with _lock:
            slugs = list(_certs)
        now = _time.monotonic()
        for slug in slugs:
            cached = _slots_cache.get(slug)
            if cached is not None and now - cached[0] < _SLOTS_TTL_S:
                out[slug] = cached[1]
                continue
            try:
                reply = link_serving_supervisor.control(slug, "fleet-org-slots", {}, timeout=8.0)
                slots = list(reply.get("slots") or []) if isinstance(reply, dict) and reply.get("ok") else []
            except Exception:
                slots = []
            _slots_cache[slug] = (now, slots)
            out[slug] = slots
        return out

    return resolve
