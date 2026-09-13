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
        return [ref.slug for ref in org_ops.list_orgs()
                if ref.slug not in ("personal", "machine")]
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


def _members_at(slug: str, record: dict) -> tuple[str, ...]:
    from tools.dashboard import membership_checkpoint as cp
    from tools.network.ledger import membership_commitment as mc

    head = record.get("ledger_head")
    state = cp._fold_at(slug, [head]) if cp._is_head(head) else cp._fold_state(slug)[0]
    return tuple(mc.member_pubs(state))


def _callables(slug: str, persona_pub: str) -> dict[str, Callable]:
    from tools.network.ledger import membership_commitment as mc

    def newest_adopted_seq():
        record = _adopted(slug)
        return int(record["seq"]) if record is not None else None

    def adopted_checkpoint_for(seq):
        record = _adopted(slug)
        if record is None or int(record["seq"]) != int(seq):
            return None
        return record

    def adopted_members_for(seq):
        record = adopted_checkpoint_for(seq)
        return _members_at(slug, record) if record is not None else None

    def membership_proof_for():
        record = _adopted(slug)
        if record is None:
            return {"v": 1, "checkpoint_seq": 0, "index": 0, "path": []}
        members = _members_at(slug, record)
        try:
            index, path = mc.inclusion_proof(members, persona_pub)
        except mc.MembershipCommitmentError:
            index, path = 0, []  # not in the adopted set yet; the peer refuses
        return {"v": 1, "checkpoint_seq": int(record["seq"]), "index": index, "path": path}

    return {
        "newest_adopted_seq": newest_adopted_seq,
        "adopted_checkpoint_for": adopted_checkpoint_for,
        "adopted_members_for": adopted_members_for,
        "membership_proof_for": membership_proof_for,
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
