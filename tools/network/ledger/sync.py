"""Peer sync — heads exchange + fetch-missing-by-hash (git-fetch semantics).

Spec ``graph://eb245082-b76`` §6–7, bead ``auto-rrzrt`` (F3). Sync is
anti-entropy over the event DAG: exchange heads (32-byte hints — the
notification plane), then move only the events the other side provably
lacks (content-addressed pull — the data plane). Because event ids are
content hashes and ``ingest`` is order-independent and idempotent, one
reconciliation pass against any single up-to-date peer catches a replica
up completely, no matter how many rounds of updates it slept through.

The module is **sans-IO**: it produces and consumes JSON-object messages
and never touches a socket. Any transport that moves bytes — a relaykit
E2E channel, a local pipe, an HTTP pair — can carry them via
:func:`encode_message` / :func:`decode_message`.

Message vocabulary (one reconciliation pass = one round trip + an
optional same-pass push)::

    sync-req   {t, v, genesis, heads}                     A -> B
    sync-resp  {t, v, genesis, heads, events, want}       B -> A
    sync-push  {t, v, events}                             A -> B (only if
                                                          B named wants)

The responder ships every event outside the ancestry of the heads the
requester declared (overshooting when it does not know a declared head —
idempotent ingest makes overshoot harmless, never incorrect), plus a
``want`` list naming the declared heads it has never seen. The requester
answers the wants with everything the responder's own heads do not reach.
After ``absorb`` + ``receive_push`` both replicas hold the identical
event set, hence — by L1 — the identical authority state.

Genesis is the org identity: two ledgers with different genesis events
are different orgs, and sync between them refuses outright rather than
merging (the DAG could not link anyway; fail loudly instead of buffering
forever). An **empty** replica may pull from anyone — cold bootstrap over
a *trusted* peer connection; untrusted bundles go through the
checkpoint-gated :meth:`~.store.LedgerStore.cold_join` instead. Pass
``expected_genesis`` to pin a fresh replica to the org it was told to
join.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable, List, Optional

from tools.network.idkit import canonical_json

from .errors import LedgerError, SchemaError
from .events import MAX_EVENT_BYTES, Event, _require_hash, require_hash_list
from .store import LedgerStore

SYNC_VERSION = 1

#: Upper bound on events in one message — a DoS guard, not a protocol
#: limit; a pass needing more simply runs again (ingest is idempotent).
MAX_SYNC_EVENTS = 65_536
#: Head-hint cap, shared with the broker surface and the F2 Settings
#: schema (a cross-pin test in registry/tests/test_topics.py holds the
#: three equal). Senders CLAMP to this — see :meth:`SyncPeer.request` —
#: so a pathologically wide DAG degrades to overshoot, never to deadlock.
MAX_SYNC_HEADS = 64

_MSG_TYPES = frozenset({"sync-req", "sync-resp", "sync-push"})
_MSG_FIELDS = {
    "sync-req": frozenset({"t", "v", "genesis", "heads"}),
    "sync-resp": frozenset({"t", "v", "genesis", "heads", "events", "want"}),
    "sync-push": frozenset({"t", "v", "events"}),
}


class SyncError(LedgerError):
    """The sync exchange is malformed or the peers are different orgs."""


def _require_hash_list(value: object, what: str, max_len: int) -> list:
    return require_hash_list(value, what, max_len, exc=SyncError)


def _require_wires(value: object, what: str = "events") -> list:
    if not isinstance(value, list) or len(value) > MAX_SYNC_EVENTS:
        raise SyncError(f"{what} must be a list of at most {MAX_SYNC_EVENTS} wire strings")
    for entry in value:
        if not isinstance(entry, str) or not entry or len(entry) > MAX_EVENT_BYTES:
            raise SyncError(f"{what} entries must be non-empty wire JSON strings")
    return value


def validate_message(message: object) -> dict:
    """Structural check of one sync message; returns it typed and vetted."""
    if not isinstance(message, dict):
        raise SyncError("sync message must be a JSON object")
    kind = message.get("t")
    if kind not in _MSG_TYPES:
        raise SyncError(f"unknown sync message type: {kind!r}")
    if set(message) != _MSG_FIELDS[kind]:
        raise SyncError(f"{kind} must carry exactly {sorted(_MSG_FIELDS[kind])}")
    if message["v"] != SYNC_VERSION:
        raise SyncError(f"unsupported sync version: {message['v']!r}")
    if "genesis" in message and message["genesis"] is not None:
        try:
            _require_hash(message["genesis"], f"{kind} genesis")
        except SchemaError as exc:
            raise SyncError(str(exc)) from None
    if "heads" in message:
        _require_hash_list(message["heads"], f"{kind} heads", MAX_SYNC_HEADS)
    if "want" in message:
        _require_hash_list(message["want"], f"{kind} want", MAX_SYNC_HEADS)
    if "events" in message:
        _require_wires(message["events"])
    return message


def encode_message(message: dict) -> bytes:
    """Canonical wire bytes for *message* (validates first)."""
    return canonical_json(validate_message(message))


def decode_message(raw) -> dict:
    """Parse and validate wire bytes into a sync message."""
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = bytes(raw).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SyncError("sync message is not valid UTF-8") from exc
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise SyncError("sync message is not valid JSON") from exc
    return validate_message(data)


def _wire(event: Event) -> str:
    return event.to_json().decode("ascii")


def _parse_events(wires: Iterable[str]) -> List[Event]:
    events = []
    for w in wires:
        try:
            raw = w.encode("ascii")
        except UnicodeEncodeError as exc:
            raise SyncError("event wire is not ASCII canonical JSON") from exc
        events.append(Event.from_json(raw))
    return events


class SyncPeer:
    """One replica's end of the sync protocol, wrapped around its store.

    Stateless between messages except for the store itself — every method
    derives what it needs from the DAG, so a peer object can serve any
    number of concurrent or repeated passes.
    """

    def __init__(self, store: LedgerStore, *, expected_genesis: Optional[str] = None):
        self.store = store
        self.expected_genesis = expected_genesis

    # -- shared -----------------------------------------------------------------

    @property
    def _genesis_id(self) -> Optional[str]:
        return self.store.ledger.genesis_id

    @property
    def _genesis_pin(self) -> Optional[str]:
        return self._genesis_id or self.expected_genesis

    def _check_genesis(self, remote_genesis: Optional[str], who: str) -> None:
        """Same-org gate, layer one: the peer's *declared* genesis.

        A ``None`` declaration (an empty peer) passes here — it is the
        events themselves that must not smuggle a foreign org in, and
        :meth:`_ingest_wires` pins every genesis in a pack regardless of
        what the sender declared.
        """
        if remote_genesis is None:
            return
        if self._genesis_id is not None and remote_genesis != self._genesis_id:
            raise SyncError(
                f"{who} genesis {remote_genesis[:12]} does not match local "
                f"replica genesis {self._genesis_id[:12]}: different orgs never merge"
            )
        if self.expected_genesis is not None and remote_genesis != self.expected_genesis:
            raise SyncError(
                f"{who} genesis {remote_genesis[:12]} does not match expected "
                f"org genesis {self.expected_genesis[:12]}: different orgs never merge"
            )

    def _declared_heads(self) -> List[str]:
        """Local heads, clamped to the protocol cap.

        A DAG wider than MAX_SYNC_HEADS declares a deterministic subset;
        the peer's subtraction just has less to subtract, so wide DAGs
        cost overshoot (idempotent) instead of wedging the exchange.
        """
        return list(self.store.heads())[:MAX_SYNC_HEADS]

    def missing_for(self, remote_heads: Iterable[str]) -> List[Event]:
        """Every local event outside the ancestry of *remote_heads*.

        Heads we do not hold contribute nothing to the subtraction —
        we overshoot rather than under-ship; ingest is idempotent.
        """
        known = [h for h in remote_heads if h in self.store.ledger]
        have = self.store.ledger.ancestry(known) if known else frozenset()
        missing = self.store.ledger.all_ids() - have
        return [self.store.get(i) for i in sorted(missing)]

    def _ingest_wires(self, wires: Iterable[str]) -> int:
        events = _parse_events(wires)
        # Same-org gate, layer two: nothing in a pack may carry a genesis
        # other than the pinned one. This holds even when the sender
        # declared genesis=null — a forged declaration cannot smuggle a
        # foreign org into a pinned (or already-anchored) replica. An
        # empty, unpinned replica accepts any genesis: that is the
        # trusted-peer cold bootstrap, stated in the module doc.
        pin = self._genesis_pin
        if pin is not None:
            for event in events:
                if event.type == "genesis" and event.event_id != pin:
                    raise SyncError(
                        f"pack carries foreign genesis {event.event_id[:12]}, "
                        f"pinned to {pin[:12]}: different orgs never merge"
                    )
        fresh = [e for e in events if e.event_id not in self.store.ledger]
        if fresh:
            self.store.append_bundle(fresh)
        return len(fresh)

    # -- requester side ------------------------------------------------------------

    def request(self) -> dict:
        """Open a pass: declare who we are (genesis) and where we are (heads)."""
        return {
            "t": "sync-req",
            "v": SYNC_VERSION,
            "genesis": self._genesis_id,
            "heads": self._declared_heads(),
        }

    def absorb(self, resp: dict) -> Optional[dict]:
        """Ingest the responder's pack; answer its wants with a push.

        Returns the ``sync-push`` message when the responder named heads
        it lacks (i.e. we hold events it needs), else ``None``.
        """
        resp = validate_message(resp)
        if resp["t"] != "sync-resp":
            raise SyncError(f"expected sync-resp, got {resp['t']!r}")
        self._check_genesis(resp["genesis"], "responder")
        self._ingest_wires(resp["events"])
        if not resp["want"]:
            return None
        # The responder's own events all arrived in this pass, so its
        # heads are known here now; everything outside their ancestry is
        # exactly what it lacks.
        push = self.missing_for(resp["heads"])
        if not push:
            return None
        return {"t": "sync-push", "v": SYNC_VERSION, "events": [_wire(e) for e in push]}

    # -- responder side ---------------------------------------------------------------

    def respond(self, req: dict) -> dict:
        """Serve one pass: pack everything the requester's heads miss."""
        req = validate_message(req)
        if req["t"] != "sync-req":
            raise SyncError(f"expected sync-req, got {req['t']!r}")
        self._check_genesis(req["genesis"], "requester")
        pack = self.missing_for(req["heads"])
        want = [h for h in req["heads"] if h not in self.store.ledger]
        return {
            "t": "sync-resp",
            "v": SYNC_VERSION,
            "genesis": self._genesis_id,
            "heads": self._declared_heads(),
            "events": [_wire(e) for e in pack],
            "want": want,
        }

    def receive_push(self, push: dict) -> int:
        """Ingest the requester's answer to our wants; returns event count."""
        push = validate_message(push)
        if push["t"] != "sync-push":
            raise SyncError(f"expected sync-push, got {push['t']!r}")
        return self._ingest_wires(push["events"])


@dataclass(frozen=True)
class SyncReport:
    """What one reconciliation pass did (offline-catch-up assertions)."""

    round_trips: int  # request/response exchanges (the latency metric)
    pulled: int  # events shipped to the requester
    pushed: int  # events the responder ingested
    fingerprint: str  # converged fold fingerprint


def sync_pair(
    requester: LedgerStore,
    responder: LedgerStore,
    *,
    now: Optional[int] = None,
) -> SyncReport:
    """One full anti-entropy pass between two stores; verifies convergence.

    Exactly one round trip (req → resp) plus, when the responder was
    behind, the same-pass push. Raises :class:`SyncError` if the replicas
    do not converge — which, per L1, can only mean different orgs (caught
    earlier) or a broken transport.
    """
    a, b = SyncPeer(requester), SyncPeer(responder)
    resp = b.respond(a.request())
    pulled = len(resp["events"])
    push = a.absorb(resp)
    pushed = b.receive_push(push) if push is not None else 0
    fp_a = requester.fold(now=now).fingerprint()
    fp_b = responder.fold(now=now).fingerprint()
    if fp_a != fp_b:
        raise SyncError("replicas did not converge after a full pass")
    return SyncReport(round_trips=1, pulled=pulled, pushed=pushed, fingerprint=fp_a)
