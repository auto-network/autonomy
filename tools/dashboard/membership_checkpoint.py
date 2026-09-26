"""Checkpoint publication — the sign-on-cadence server prep (auto-tmers).

Design of record: graph://da0dd9fb-e75 + the auto-tmers bead. At sign-on the
dashboard decides, per org, whether a fresh membership checkpoint is due —
by a LOCAL comparison against a cached last-adopted record, never a registry
call — and if so assembles the UNSIGNED checkpoint record. The browser
ceremony signs it (root for a seq-0 seed, persona otherwise) and POSTs it;
:func:`record_adopted` updates the cache on success.

Nothing here signs or reaches the network. It folds the org ledger, reads
and writes the local cache, and returns a decision the browser acts on —
the same shape as the serve-cert status prep.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from tools.graph import settings_ops
from tools.graph.schemas.network_identity import (
    NETWORK_CHECKPOINT_CACHE_REVISION,
    NETWORK_CHECKPOINT_CACHE_SET_ID,
)
from tools.network.ledger import LedgerStore, org_ledger_db_path
from tools.network.ledger import membership_commitment as mc

#: What the browser must sign an assembled record with.
SIGN_WITH_ROOT = "root"        # seq-0 seed / reset
SIGN_WITH_PERSONA = "persona"  # every advancing checkpoint


@dataclass(frozen=True)
class CheckpointDecision:
    """The per-org outcome of the sign-on check.

    ``action`` is one of:
      * ``up-to-date`` — the registry (as cached) already reflects the fold.
      * ``assemble`` — ``record`` is the UNSIGNED checkpoint; sign it with
        ``sign_with`` and POST it, then call :func:`record_adopted`.
      * ``not-checkpointer`` — this persona holds no membership:checkpoint
        scope; nothing to do.
      * ``not-eligible`` — this persona is a checkpointer in the current
        fold but is not in the previous adopted ``checkpointers_root``, so it
        cannot be the first to publish the checkpoint that includes it; an
        existing checkpointer must go first.
    """

    action: str
    record: Optional[dict] = None
    sign_with: Optional[str] = None
    reason: Optional[str] = None


def _fold_state(org: str):
    store = LedgerStore(org_ledger_db_path(org))
    try:
        return store.fold(), tuple(store.heads())
    finally:
        store.close()


def _fold_at(org: str, heads):
    store = LedgerStore(org_ledger_db_path(org))
    try:
        return store.fold(heads=list(heads))
    finally:
        store.close()


def _cached_payload(org: str) -> Optional[dict]:
    """The checkpoint cache row's payload at the current revision (a
    revision-1 row upgrades on read), or None. A missing cache for ANY reason
    reads as "no adopted checkpoint known", the seed case; correctness never
    rests on the cache (the registry's seq+1 rule is the gate)."""
    try:
        members = settings_ops.read_set(
            NETWORK_CHECKPOINT_CACHE_SET_ID, org=org, peers=[],
            target_revision=NETWORK_CHECKPOINT_CACHE_REVISION, key_equals="default")
    except Exception:
        return None
    for member in members:
        if member.key == "default" and isinstance(member.payload, dict):
            return member.payload
    return None


def _cached_adopted(org: str) -> Optional[dict]:
    """The newest adopted checkpoint record, or None."""
    payload = _cached_payload(org)
    return payload.get("record") if payload is not None else None


def adopted_history(org: str) -> dict[int, dict]:
    """Every retained adopted record by seq (OrgAdmission.tla E-any-adm:
    the verifier accepts a proof under any of these at or after the prover's
    admission, and proves back under the peer's seq from them)."""
    payload = _cached_payload(org)
    if payload is None:
        return {}
    history = payload.get("history")
    if not isinstance(history, dict):
        record = payload.get("record")
        return {int(payload["seq"]): record} if isinstance(record, dict) else {}
    out: dict[int, dict] = {}
    for key, record in history.items():
        if isinstance(key, str) and key.isdigit() and isinstance(record, dict):
            out[int(key)] = record
    return out


def adopted_record_for(org: str, seq: int) -> Optional[dict]:
    return adopted_history(org).get(int(seq))


def checkpoint_due(org: str, persona_pub: str, *, ts: int,
                   genesis_id: str,
                   org_uuid: Optional[str] = None) -> CheckpointDecision:
    """The sign-on decision for one org. Pure of network and signing.

    *org* is the local ledger slug (it names the ledger DB); *org_uuid* is the
    org's registry identity and is what the assembled ``record["org"]`` must
    carry, because the registry — and the dashboard forward route — reject a
    record whose org is not the path uuid. When *org_uuid* is omitted the slug
    stands in, which only holds where slug and uuid coincide (the unit fold
    fixtures); a registered org must pass its uuid.

    *persona_pub* is this node's persona in the org; *genesis_id* anchors a
    seed's ``prev``. *ts* stamps an assembled record.
    """
    record_org = org_uuid or org
    state, _heads = _fold_state(org)
    members_root = mc.members_root(state)
    checkpointers_root = mc.checkpointers_root(state)
    ledger_head = _first_head(state)

    cached = _cached_adopted(org)
    if cached is not None \
            and cached.get("members_root") == members_root \
            and cached.get("checkpointers_root") == checkpointers_root:
        return CheckpointDecision("up-to-date")

    # Only a checkpointer can sign, so past this point the persona must hold
    # the scope. (Determined from the fold already computed.)
    if persona_pub not in mc.checkpointer_pubs(state):
        return CheckpointDecision("not-checkpointer")

    if cached is None:
        # No adopted checkpoint known locally: this is the seed (seq 0),
        # root-signed, anchored at genesis, no proof (the root form). The
        # browser fills `signer` (the root pub) and `sig`. If the registry is
        # in fact already seeded, the POST fails and the browser does a
        # one-time read to populate the cache, then re-evaluates.
        record = {
            "v": mc.CHECKPOINT_VERSION,
            "org": record_org,
            "seq": 0,
            "prev": genesis_id,
            "ledger_head": ledger_head,
            "members_root": members_root,
            "checkpointers_root": checkpointers_root,
            "ts": ts,
        }
        return CheckpointDecision("assemble", record=record,
                                  sign_with=SIGN_WITH_ROOT)

    # Advancing checkpoint: prove this persona under the PREVIOUS
    # checkpointers_root, reconstructed by re-folding at the cached record's
    # ledger_head — the checkpointer set as of the adopted checkpoint.
    prev_state = _fold_at(org, [cached["ledger_head"]]) \
        if _is_head(cached.get("ledger_head")) else state
    prev_checkpointers = mc.checkpointer_pubs(prev_state)
    if persona_pub not in prev_checkpointers:
        return CheckpointDecision(
            "not-eligible",
            reason=("this persona became a checkpointer after the last "
                    "adopted checkpoint; an existing checkpointer must "
                    "publish the checkpoint that first includes it"))
    index, path = mc.inclusion_proof(prev_checkpointers, persona_pub)
    record = {
        "v": mc.CHECKPOINT_VERSION,
        "org": record_org,
        "seq": cached["seq"] + 1,
        "prev": mc.checkpoint_hash(cached),
        "ledger_head": ledger_head,
        "members_root": members_root,
        "checkpointers_root": checkpointers_root,
        "ts": ts,
        "signer": persona_pub,
        "proof": path,
        "proof_index": index,
    }
    return CheckpointDecision("assemble", record=record,
                              sign_with=SIGN_WITH_PERSONA)


def checkpoint_status(org: str) -> dict:
    """The persona-INDEPENDENT checkpoint verdict for the unlock plan.

    ``needed`` is true when the fold's roots differ from the last adopted
    checkpoint — i.e. a fresh checkpoint would change the committed view.
    ``checkpointer_pubs`` is the permission set: a client whose derived persona
    is not in it can never publish and must never attempt. No persona, no
    network, no signing here — the unlock plan carries both so the client gates
    the checkpoint step LOCALLY (needed AND my persona in checkpointer_pubs).
    """
    try:
        state, _heads = _fold_state(org)
    except Exception:
        return {"needed": False, "checkpointer_pubs": [], "reason": "no-ledger"}
    members_root = mc.members_root(state)
    checkpointers_root = mc.checkpointers_root(state)
    cached = _cached_adopted(org)
    up_to_date = (cached is not None
                  and cached.get("members_root") == members_root
                  and cached.get("checkpointers_root") == checkpointers_root)
    return {
        "needed": not up_to_date,
        "checkpointer_pubs": list(mc.checkpointer_pubs(state)),
        "members_root": members_root,
        # The seq this node has adopted, or None when no checkpoint has been
        # adopted at all. None is what makes a PERSONA-signed serving
        # credential unusable for this org: it authenticates only at a registry
        # that has adopted the org's seed, and the provisioning route refuses
        # to store one before then.
        "adopted_seq": (cached or {}).get("seq") if cached is not None else None,
    }


def record_adopted(org: str, signed_record: dict) -> None:
    """Retain an adopted checkpoint (after a successful publish, a fold
    adoption of a registry or bundle state, or a verified peer record). The
    newest retained record is the row's ``record``; the history keeps the
    newest NETWORK_CHECKPOINT_HISTORY_LIMIT records by seq. Recording a seq
    already retained replaces that entry (a signed form may replace a state
    tuple); recording an older seq never changes which record is newest
    (NoRegression)."""
    from tools.graph.schemas.network_identity import NETWORK_CHECKPOINT_HISTORY_LIMIT

    seq = int(signed_record["seq"])
    history = adopted_history(org)
    history[seq] = dict(signed_record)
    kept = sorted(history, reverse=True)[:NETWORK_CHECKPOINT_HISTORY_LIMIT]
    newest = kept[0]
    settings_ops.upsert_by_key(
        NETWORK_CHECKPOINT_CACHE_SET_ID, NETWORK_CHECKPOINT_CACHE_REVISION,
        "default",
        {"seq": newest, "record": history[newest],
         "history": {str(k): history[k] for k in kept}},
        org=org,
    )


# -- helpers -------------------------------------------------------------------


def _first_head(state) -> str:
    heads = sorted(state.heads) if getattr(state, "heads", None) else []
    return heads[0] if heads else state.genesis_id


def _is_head(value) -> bool:
    return isinstance(value, str) and len(value) == 64


def adopt_after_membership_events(slugs=None, *, adopt=None) -> dict[str, dict]:
    """The AutoAdopt machine step (OrgAdmissionBundleBound.tla): after a pull
    materialized ledger events, for every org this node syncs whose current
    fold no longer matches its newest retained record, read the registry
    once and adopt its record by fold. An org whose fold still matches costs
    nothing (no registry read): a checkpoint newer than the fold cannot
    exist. Returns slug -> {"skipped": reason} | the adoption result."""
    if slugs is None:
        from tools.dashboard import org_sync_channels
        slugs = sorted(org_sync_channels.report())
    if adopt is None:
        from tools.dashboard.network_routes import _adopt_registry_checkpoint as adopt
    out: dict[str, dict] = {}
    for slug in slugs:
        status = checkpoint_status(slug)
        if not status.get("needed"):
            out[slug] = {"skipped": "fold matches the newest retained record"}
            continue
        try:
            out[slug] = adopt(slug)
        except Exception as exc:  # noqa: BLE001
            out[slug] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return out
