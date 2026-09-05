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


def _cached_adopted(org: str) -> Optional[dict]:
    # A missing cache for ANY reason — no row, or a store not yet initialized
    # on a fresh node — reads as "no adopted checkpoint known", which is the
    # seed case. Correctness never rests on the cache (the registry's seq+1
    # rule is the gate), so failing to None here is safe.
    try:
        row = settings_ops.read_set_key(
            NETWORK_CHECKPOINT_CACHE_SET_ID, "default", org=org)
    except Exception:
        return None
    if row is None:
        return None
    payload = row.get("payload") or {}
    return payload.get("record")


def checkpoint_due(org: str, persona_pub: str, *, ts: int,
                   genesis_id: str) -> CheckpointDecision:
    """The sign-on decision for one org. Pure of network and signing.

    *persona_pub* is this node's persona in the org; *genesis_id* anchors a
    seed's ``prev``. *ts* stamps an assembled record.
    """
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
            "org": org,
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
        "org": org,
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


def record_adopted(org: str, signed_record: dict) -> None:
    """Cache a checkpoint the registry has adopted (called after a successful
    POST, or a one-time registry read that discovers a newer state)."""
    settings_ops.upsert_by_key(
        NETWORK_CHECKPOINT_CACHE_SET_ID, NETWORK_CHECKPOINT_CACHE_REVISION,
        "default", {"seq": signed_record["seq"], "record": signed_record},
        org=org,
    )


# -- helpers -------------------------------------------------------------------


def _first_head(state) -> str:
    heads = sorted(state.heads) if getattr(state, "heads", None) else []
    return heads[0] if heads else state.genesis_id


def _is_head(value) -> bool:
    return isinstance(value, str) and len(value) == 64
