"""Client-side equivocation-witness verification (F4, L5).

Spec ``graph://eb245082-b76`` §6 role 2, bead ``auto-12jah``. This is the
member's half of the anti-fork role: it consumes the registry witness
(:mod:`registry.witness`) — signed, append-only head-set attestations — and
turns any contradiction into an independently-checkable proof. It mirrors
the broker client the same way :mod:`.broker` mirrors the registry mailbox:
a signed-envelope :class:`WitnessClient` over an injected transport, plus a
pure verifier (:class:`WitnessJournal`) that needs only attestations and
the local DAG.

The trust model is Certificate-Transparency's, reduced to hashes:

- The witness key is **pinned** (``GET /v1/witness/pubkey``, then trust on
  first use / out-of-band). Every check verifies against the pinned key,
  never the key a response advertises — otherwise a split-view server would
  simply sign each half under a different key and nothing would collide.
- A member keeps a **journal**: the last attestation it accepted. A fresh
  served attestation must *extend* that journal — same chain, forward only.
- Two witness-signed attestations that cannot both belong to one honest,
  append-only chain are an :class:`EquivocationProof`: self-contained
  evidence (the two signed responses) anyone can re-check against the
  pinned key. That is the "provable transcript".

Two contradictions are self-contained proofs (signatures alone):

- **split-seq** — two entries at the *same* ``(org, topic, seq)`` with
  different ids. The witness signed two histories at one position.
- **fork-prev** — an entry at ``seq = N+1`` whose ``prev`` is not the id of
  the entry the member holds signed at ``seq = N``. The witness built the
  chain on a different ``seq = N`` entry than the one it also signed.

Two more are caught with the local DAG (not portable by signature alone,
so surfaced as their own errors, not as ``EquivocationProof``):

- **retraction** — a new head-set drops a previously-witnessed head that no
  descendant supersedes (:func:`dominates` is false). Append-only at the
  authority-frontier layer: the frontier only moves forward.
- **stale** — a served "tip" older than one already witnessed. Refused, not
  proven (an older entry can honestly exist; only the *claim* that it is the
  current tip is hostile, and that claim is not signed).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from tools.network.idkit import DelegationCert, KeyPair
from tools.network.registry.signing import sign_request
from tools.network.registry.witness import (
    WitnessFormatError,
    entry_id,
    validate_entry,
    verify_attestation,
)

from .errors import LedgerError
from .store import LedgerStore


class WitnessError(LedgerError):
    """Base for witness-verification failures."""


def _verified_entry(attestation: dict, witness_pub: str) -> dict:
    """Verify against the pinned key, surfacing one uniform error type.

    :func:`registry.witness.verify_attestation` raises its own
    ``WitnessFormatError`` (an idkit ``MalformedError``); the client API
    presents every witness failure as a :class:`WitnessError` so callers
    catch one hierarchy.
    """
    try:
        return verify_attestation(attestation, witness_pub)
    except WitnessFormatError as exc:
        raise WitnessError(str(exc)) from exc


class WitnessEquivocation(WitnessError):
    """A proven fork: carries a self-contained :class:`EquivocationProof`."""

    def __init__(self, message: str, proof: "EquivocationProof"):
        super().__init__(message)
        self.proof = proof


class WitnessRetraction(WitnessError):
    """A head-set that drops a non-superseded head (DAG-checked)."""


class WitnessStale(WitnessError):
    """A served tip older than one already witnessed — refused, not proven."""


class WitnessGap(WitnessError):
    """An advance skipped seqs; fetch the intervening chain and re-admit."""


def dominates(store: LedgerStore, new_heads, old_heads) -> bool:
    """Does *new_heads* supersede *old_heads* in the local DAG?

    True iff every old head is an ancestor-or-self of some new head — i.e.
    ``ancestry(new) ⊇ old``. That is exactly "no previously-witnessed head
    dropped without a descendant replacing it": a forward move of the
    authority frontier. All ids must be present locally so ancestry is
    computable; an unknown id raises (sync first) rather than risking a
    stale replica mislabelling a legitimate advance as a retraction.
    """
    ledger = store.ledger
    for h in list(new_heads) + list(old_heads):
        if h not in ledger:
            raise WitnessError(
                f"cannot judge supersession: event {h[:12]} not in local replica "
                "(sync the DAG before verifying the witness)"
            )
    anc = ledger.ancestry(new_heads)
    return all(h in anc for h in old_heads)


@dataclass(frozen=True)
class EquivocationProof:
    """Two witness-signed attestations that cannot both be honest.

    Self-contained: :meth:`verify` re-checks both signatures against a
    pinned key and re-derives the contradiction from the signed content
    alone. :meth:`transcript` is the portable artifact — the two signed
    responses — that a member hands to anyone, on-ledger or off.
    """

    a: dict
    b: dict
    kind: str  # "split-seq" | "fork-prev"

    def verify(self, witness_pub: str) -> bool:
        """True iff both attestations verify under *witness_pub* and the
        stated contradiction genuinely holds. Any tampering — a doctored
        entry, a swapped signature, a mislabelled kind — makes this False,
        so a proof is only as good as it is checkable."""
        try:
            ea = verify_attestation(self.a, witness_pub)
            eb = verify_attestation(self.b, witness_pub)
        except Exception:
            return False
        if ea["org"] != eb["org"] or ea["topic"] != eb["topic"]:
            return False
        if self.kind == "split-seq":
            return ea["seq"] == eb["seq"] and entry_id(ea) != entry_id(eb)
        if self.kind == "fork-prev":
            # b sits one step above a, but roots on a different seq-N entry
            # than the one a IS — two signed seq-N histories.
            return eb["seq"] == ea["seq"] + 1 and eb["prev"] != entry_id(ea)
        return False

    def transcript(self) -> dict:
        """The portable evidence bundle: kind + the two signed responses."""
        return {"kind": self.kind, "a": self.a, "b": self.b}


class WitnessJournal:
    """A member's running record of the witness chain for one topic.

    Pins the witness key and remembers the last attestation admitted. Feed
    it fresh attestations; it advances on a clean forward extension and
    raises on any contradiction. Pure — no I/O, no clock — so it verifies
    identically over a live transport, a gossiped peer tip, or a replayed
    transcript.
    """

    def __init__(self, witness_pub: str, *, org: Optional[str] = None,
                 topic: Optional[str] = None):
        self.witness_pub = witness_pub
        self.org = org
        self.topic = topic
        self._last: Optional[dict] = None  # last admitted attestation

    @property
    def last(self) -> Optional[dict]:
        return self._last

    @property
    def seq(self) -> int:
        """Highest witnessed seq (0 before anything is admitted)."""
        return 0 if self._last is None else self._last["entry"]["seq"]

    def _verify(self, attestation: dict) -> dict:
        entry = _verified_entry(attestation, self.witness_pub)
        if self.org is not None and entry["org"] != self.org:
            raise WitnessError("attestation org does not match this journal")
        if self.topic is not None and entry["topic"] != self.topic:
            raise WitnessError("attestation topic does not match this journal")
        return entry

    def reconcile(self, attestation: dict) -> Optional[EquivocationProof]:
        """Compare a *claimed* attestation against the journal WITHOUT
        advancing. Returns a proof if it provably contradicts what we hold,
        else ``None`` (consistent, or simply ahead — admit the gap then).

        This is the gossip / cross-member primitive: two members exchange
        their witnessed tips and each calls ``reconcile`` on the other's —
        a split-view server is caught the moment its two victims compare.
        """
        entry = self._verify(attestation)
        if self._last is None:
            return None
        last_entry = self._last["entry"]
        last_id = entry_id(last_entry)  # canonical, not the served field
        incoming_id = entry_id(entry)
        if entry["seq"] == last_entry["seq"]:
            if incoming_id != last_id:
                return EquivocationProof(self._last, attestation, "split-seq")
            return None
        # fork-prev is a chain break at an adjacent seq, and it must be
        # detectable from EITHER side of the split — the whole point is that
        # both victims of an equivocation catch it, not just whichever one
        # happens to hold the lower entry. So check both orientations, always
        # ordering the proof (a, b) as (lower seq, higher seq) so the entry
        # at seq N and the entry at seq N+1 that fails to chain to it are
        # compared the same way regardless of which one we were holding.
        if entry["seq"] == last_entry["seq"] + 1 and entry["prev"] != last_id:
            # we hold N; shown an N+1 whose prev does not point back to it
            return EquivocationProof(self._last, attestation, "fork-prev")
        if entry["seq"] == last_entry["seq"] - 1 and last_entry["prev"] != incoming_id:
            # we hold N+1; shown an N that our prev does not point back to
            return EquivocationProof(attestation, self._last, "fork-prev")
        return None

    def admit(self, attestation: dict, store: LedgerStore) -> dict:
        """Admit one attestation as the journal's next step; advance or raise.

        Enforces, against the pinned key and the local DAG:

        - baseline (empty journal): accept any seq as the anchor;
        - one-step forward: ``seq == last+1`` and ``prev == last id``
          (a mismatched prev is a proven **fork-prev**);
        - same seq: a differing id is a proven **split-seq**; an identical
          re-serve is a no-op;
        - an older seq is **stale**; a skipped seq is a **gap**;
        - the head-set must *supersede* the last (``dominates``) — a
          non-superseding drop is a **retraction**.
        """
        entry = self._verify(attestation)
        if self._last is None:
            self._last = attestation
            return attestation
        last_entry = self._last["entry"]
        last_id = entry_id(last_entry)  # canonical, not the served field
        seq, last_seq = entry["seq"], last_entry["seq"]

        if seq == last_seq:
            if entry_id(entry) != last_id:
                proof = EquivocationProof(self._last, attestation, "split-seq")
                raise WitnessEquivocation(
                    f"witness signed two entries at seq {seq}", proof
                )
            return self._last  # identical re-serve
        if seq < last_seq:
            raise WitnessStale(
                f"served tip seq {seq} is behind the witnessed seq {last_seq}"
            )
        if seq > last_seq + 1:
            raise WitnessGap(
                f"advance skips from seq {last_seq} to {seq}; fetch the chain"
            )
        # seq == last_seq + 1: a genuine one-step extension must chain.
        if entry["prev"] != last_id:
            proof = EquivocationProof(self._last, attestation, "fork-prev")
            raise WitnessEquivocation(
                f"entry at seq {seq} does not chain to the witnessed seq {last_seq}",
                proof,
            )
        if not dominates(store, entry["heads"], last_entry["heads"]):
            raise WitnessRetraction(
                f"head-set at seq {seq} drops a previously-witnessed head "
                "without a superseding descendant (append-only violation)"
            )
        self._last = attestation
        return attestation

    def admit_chain(self, attestations: List[dict], store: LedgerStore) -> int:
        """Admit a contiguous run of attestations (a ``witness/since`` page);
        returns how many advanced the journal. Ordered by seq."""
        advanced = 0
        for att in sorted(attestations, key=lambda a: a["entry"]["seq"]):
            before = self.seq
            self.admit(att, store)
            if self.seq > before:
                advanced += 1
        return advanced


def witnessed_fold(store: LedgerStore, attestation: dict, witness_pub: str,
                   *, now: Optional[int] = None):
    """Fold the local ledger *as of a witnessed head-set* (the F7 seam).

    "Trustee state as of witnessed head H" (spec §5, L7 circularity
    defense) resolves deterministically to ``fold(heads=H)`` — but only
    against a head-set that actually carries the witness signature, so a
    forged branch (never witnessed) can never be the ``as-of`` point.
    Returns the :class:`~.fold.FoldState` at the attested heads.
    """
    entry = _verified_entry(attestation, witness_pub)
    for h in entry["heads"]:
        if h not in store.ledger:
            raise WitnessError(
                f"witnessed head {h[:12]} not in local replica; sync before folding"
            )
    return store.fold(heads=entry["heads"], now=now)


# -- signed-envelope client (mirrors broker.BrokerClient) ---------------------

Transport = Callable[[str, str, dict], Tuple[int, dict]]


class WitnessClient:
    """Signed-envelope client for one org's witness surface.

    Same transport contract as :class:`~.broker.BrokerClient`
    (``transport(method, path, json) -> (status, body)``) so it runs over
    httpx in production and a FastAPI ``TestClient`` in tests. Every call is
    a signed envelope through the I4 gate with the per-topic scope.
    """

    def __init__(
        self,
        transport: Transport,
        org_uuid: str,
        key: KeyPair,
        *,
        cert: Optional[DelegationCert] = None,
        now_fn: Optional[Callable[[], float]] = None,
    ):
        self._transport = transport
        self.org_uuid = org_uuid
        self._key = key
        self._cert = cert
        self._now_fn = now_fn or time.time

    def _call(self, path: str, payload: dict) -> dict:
        envelope = sign_request(
            self._key, "POST", path, payload, ts=int(self._now_fn()), cert=self._cert
        )
        status, body = self._transport("POST", path, envelope)
        if status not in (200, 201):
            raise WitnessError(f"witness refused POST {path}: {status} {body!r}")
        if not isinstance(body, dict):
            raise WitnessError(f"witness returned a non-object body for POST {path}")
        return body

    def _path(self, topic: str, suffix: str) -> str:
        return f"/v1/orgs/{self.org_uuid}/topics/{topic}/witness{suffix}"

    def publish(self, topic: str, heads: List[str]) -> dict:
        """Publish the observed head-set; returns the signed tip attestation."""
        return self._call(self._path(topic, ""), {"heads": sorted(set(heads))})

    def head(self, topic: str) -> Optional[dict]:
        """The current signed head-set, or ``None`` if the log is empty."""
        return self._call(self._path(topic, "/head"), {})["attestation"]

    def since(self, topic: str, since: int = 0) -> Tuple[List[dict], int]:
        """Signed chain entries after *since*; returns ``(entries, next_since)``."""
        body = self._call(self._path(topic, "/since"), {"since": since})
        return body["entries"], body["next_since"]

    def sync(self, topic: str, journal: WitnessJournal, store: LedgerStore) -> int:
        """One reconciliation pass: pull new chain entries and admit them.

        Returns how many entries advanced the journal. Raises through the
        verifier on any equivocation/retraction — the point of the call.
        """
        entries, _next = self.since(topic, journal.seq)
        return journal.admit_chain(entries, store)
