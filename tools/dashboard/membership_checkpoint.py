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
    chain = mc.chain_record_for(cached)
    if chain is None:
        return CheckpointDecision(
            "chain-missing",
            reason=("the newest adopted checkpoint was adopted by fold without its "
                    "signed bytes; read the registry's record first"))
    prev_checkpointers = previous_checkpointers(org, cached, state)
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
        "prev": mc.checkpoint_hash(chain),
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


def previous_checkpointers(org: str, cached: dict, state=None):
    """The checkpointer set as of the adopted record *cached*: the fold at
    its ledger_head (the current fold when that head is not a head)."""
    if _is_head(cached.get("ledger_head")):
        prev_state = _fold_at(org, [cached["ledger_head"]])
    else:
        prev_state = state if state is not None else _fold_state(org)[0]
    return mc.checkpointer_pubs(prev_state)


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


# ── Checkpoint at admission (OrgAdmission.tla P2) ─────────────────────────
#: Bounded retry when the registry moved between our read and our POST
#: (two checkpointers, or two admissions close together, on one prev).
PUBLISH_RETRIES = 3


def _post_checkpoint_to_registry(binding: dict, record: dict) -> tuple[int, str]:
    """POST one record to the org's registry; (status, text). Seam."""
    import httpx

    with httpx.Client(base_url=str(binding["registry_url"]), verify=True, timeout=15.0) as client:
        resp = client.post(f"/v1/orgs/{binding['org_uuid']}/membership-checkpoints", json=record)
    return resp.status_code, resp.text


def _delegate_signer(org: str):
    """(delegate KeyPair, grant event wire) for this node's hot delegate in
    *org* when it carries the checkpoint scope; else (None, why)."""
    from tools.dashboard import org_storage_delegate

    key = org_storage_delegate.signing_key(org)
    if key is None:
        return None, "no organization delegate on this node"
    metadata = org_storage_delegate.prepare(org)["delegate_metadata"]
    grant_id = metadata.get("grant_event_id")
    if not grant_id:
        return None, "the delegate's grant is not recorded"
    store = LedgerStore(org_ledger_db_path(org))
    try:
        grant = store.get(grant_id)
    except KeyError:
        return None, "the delegate's grant is not in the ledger"
    finally:
        store.close()
    if mc.CHECKPOINT_SCOPE not in list(grant.payload.get("scope") or []):
        return None, "the delegate does not carry the checkpoint scope"
    return (key, grant.to_json().decode("utf-8")), None


def publish_after_membership_change(
    org: str, *, signer=None, post=None, adopt_registry=None, now: Optional[int] = None,
) -> dict:
    """The step that admits (or removes) also publishes the checkpoint that
    reflects it, with no persona present: signed by this node's hot delegate
    when it carries the checkpoint scope and its granting persona is in the
    previous checkpointers root. Fires after any append; when the current
    fold's roots still match the newest retained record there is nothing to
    publish and nothing is read.

    *signer* is (delegate KeyPair, grant wire) (default: this node's
    delegate); *post* posts a record to the registry (default: HTTP);
    *adopt_registry* re-reads and fold-adopts the registry's record on a
    seq/prev refusal (default: network_routes._adopt_registry_checkpoint).
    Returns {"action": "published"|"up-to-date"|"skipped"|"refused",
    ...}. Never raises."""
    import time as _time

    from tools.dashboard.network_routes import NETWORK_BINDING_SET_ID, _first_member

    try:
        status = checkpoint_status(org)
        if not status.get("needed"):
            return {"action": "up-to-date", "seq": status.get("adopted_seq")}
        cached = _cached_adopted(org)
        if cached is None:
            return {"action": "skipped", "reason": "no adopted checkpoint to advance (seed pending)"}
        binding_member = _first_member(NETWORK_BINDING_SET_ID, org)
        binding = binding_member.payload if binding_member is not None else None
        if not isinstance(binding, dict) or not isinstance(binding.get("org_uuid"), str):
            return {"action": "skipped", "reason": "no registry binding"}
        if signer is None:
            signer, why = _delegate_signer(org)
            if signer is None:
                return {"action": "skipped", "reason": why}
        key, grant_wire = signer
        persona = mc.checkpoint_signer_persona({"grant": grant_wire})
        post = post or _post_checkpoint_to_registry
        if adopt_registry is None:
            from tools.dashboard.network_routes import _adopt_registry_checkpoint as adopt_registry
        store = LedgerStore(org_ledger_db_path(org))
        try:
            genesis_id = store.ledger.genesis_id
        finally:
            store.close()
        last = "no attempt"
        for _attempt in range(PUBLISH_RETRIES):
            chain = mc.chain_record_for(cached)
            if chain is None:
                # Adopted by fold without the signed bytes (B1): the registry
                # serves its stored record with the tuple; one read.
                adopt_registry(org)
                cached = _cached_adopted(org)
                chain = mc.chain_record_for(cached) if cached is not None else None
                if chain is None:
                    return {"action": "skipped", "reason": (
                        "the newest adopted checkpoint has no signed bytes to chain from")}
            state, _heads = _fold_state(org)
            prev_checkpointers = previous_checkpointers(org, cached, state)
            if persona not in prev_checkpointers:
                return {"action": "skipped", "reason": (
                    "the delegate's persona is not in the previous checkpointers root")}
            record = mc.build_delegate_checkpoint(
                org=binding["org_uuid"], seq=int(cached["seq"]) + 1,
                prev=mc.checkpoint_hash(chain), ledger_head=_first_head(state),
                members_root_hex=mc.members_root(state),
                checkpointers_root_hex=mc.checkpointers_root(state),
                ts=int(now if now is not None else _time.time()), delegate=key,
                grant_wire=grant_wire, genesis_id=genesis_id,
                prev_checkpointer_pubs=prev_checkpointers,
            )
            code, text = post(binding, record)
            if code == 201:
                record_adopted(org, record)
                return {"action": "published", "seq": record["seq"], "sign_with": "delegate"}
            last = f"registry refused ({code}): {text[:200]}"
            if code == 403 and any(marker in text for marker in mc.CHAIN_REFUSALS):
                # The registry moved past our prev: adopt its record and
                # re-assemble on it (F3), still including every member so far.
                adopt_registry(org)
                newer = _cached_adopted(org)
                if newer is None or newer.get("seq") == cached.get("seq"):
                    break
                cached = newer
                continue
            break
        return {"action": "refused", "reason": last}
    except Exception as exc:  # noqa: BLE001 — the admission stands regardless
        return {"action": "refused", "reason": f"{type(exc).__name__}: {exc}"}
