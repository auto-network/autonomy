"""The ledger DAG container.

Holds structurally verified events keyed by id. Structural admission checks
(signature, parent existence, HLC monotonicity, genesis rules) live here;
*semantic* validity — did the author hold the authority? — is the fold's
job (:mod:`.fold`), because a replica must be able to hold and propagate
events it judges invalid so every replica converges on the same judgement.

``add`` is idempotent (ids are content hashes, so re-adding the same id is
always the same bytes) and CRDT-shaped: any two replicas holding the same
event set are identical regardless of insertion order.
"""

from __future__ import annotations

from tools.network.dag_tag import AUTHORITY, tag_dag

from typing import Dict, Iterable, List, Optional, Set

from .errors import CausalityError, GenesisError, LedgerError, UnknownParentError
from .events import Event


class Ledger:
    def __init__(self):
        self._events: Dict[str, Event] = {}
        self._children: Dict[str, Set[str]] = {}
        self._genesis_id: Optional[str] = None

    # -- read side -------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._events)

    def __contains__(self, event_id: str) -> bool:
        return event_id in self._events

    def get(self, event_id: str) -> Event:
        try:
            return self._events[event_id]
        except KeyError:
            raise UnknownParentError(f"unknown event id {event_id!r}") from None

    @property
    def genesis_id(self) -> Optional[str]:
        return self._genesis_id

    @property
    def genesis(self) -> Event:
        if self._genesis_id is None:
            raise GenesisError("ledger has no genesis event")
        return self._events[self._genesis_id]

    def all_ids(self) -> frozenset:
        return frozenset(self._events)

    def events(self) -> List[Event]:
        """All events, sorted by id (insertion-order independent)."""
        return [self._events[i] for i in sorted(self._events)]

    def heads(self) -> tuple:
        """Ids of events no other event names as a parent, sorted."""
        return tuple(sorted(i for i in self._events if not self._children.get(i)))

    @tag_dag(AUTHORITY)
    def ancestry(self, event_ids: Iterable[str]) -> frozenset:
        """The ancestor closure of *event_ids* (inclusive)."""
        seen: Set[str] = set()
        stack = [i for i in event_ids]
        while stack:
            eid = stack.pop()
            if eid in seen:
                continue
            event = self.get(eid)
            seen.add(eid)
            stack.extend(p for p in event.parents if p not in seen)
        return frozenset(seen)

    # -- write side --------------------------------------------------------------

    def add(self, event: Event) -> str:
        """Admit *event* after structural verification; return its id.

        Idempotent for an id already held. Raises:

        - :class:`SignatureError` — author signature does not verify
        - :class:`UnknownParentError` — a parent is not in this ledger
        - :class:`CausalityError` — HLC does not advance past every parent
        - :class:`GenesisError` — second genesis, or non-genesis before one
        """
        if event.event_id in self._events:
            return event.event_id

        event.verify_sig()

        if event.type == "genesis":
            if self._genesis_id is not None:
                raise GenesisError("ledger already has a genesis event")
            if event.author_key != event.payload["root_pub"]:
                raise GenesisError("genesis must be self-signed by root_pub")
        else:
            if self._genesis_id is None:
                raise GenesisError("ledger has no genesis; cannot admit events")
            for parent_id in event.parents:
                parent = self._events.get(parent_id)
                if parent is None:
                    raise UnknownParentError(
                        f"event {event.event_id[:12]} names unknown parent {parent_id[:12]}"
                    )
                if not event.hlc > parent.hlc:
                    raise CausalityError(
                        f"event {event.event_id[:12]} hlc must strictly exceed parent"
                        f" {parent_id[:12]} hlc"
                    )

        self._events[event.event_id] = event
        self._children.setdefault(event.event_id, set())
        for parent_id in event.parents:
            self._children.setdefault(parent_id, set()).add(event.event_id)
        if event.type == "genesis":
            self._genesis_id = event.event_id
        return event.event_id

    def append(self, event: Event) -> str:
        """The store-compatible name for :meth:`add`.

        A :class:`~tools.network.ledger.store.LedgerStore` persists on
        ``append``; a bare in-memory ledger has no disk to reach, so here it is
        simply :meth:`add`. Both expose the same verb so a writer takes one
        interface — ``append`` / :attr:`genesis_id` / :meth:`heads` — and code
        that must be durable is handed the STORE without any branch on type.
        """
        return self.add(event)

    def ingest(self, events: Iterable[Event]) -> List[str]:
        """Admit a batch in ANY order (sync bundles arrive unordered).

        Buffers events whose parents have not arrived yet and retries until
        a fixpoint. Raises :class:`LedgerError` if events remain unresolvable
        (missing parents outside the batch) or any event fails structural
        verification.
        """
        pending = list(events)
        added: List[str] = []
        while pending:
            progressed = False
            deferred: List[Event] = []
            for event in pending:
                ready = event.type == "genesis" or (
                    self._genesis_id is not None
                    and all(p in self._events for p in event.parents)
                )
                if ready:
                    added.append(self.add(event))
                    progressed = True
                else:
                    deferred.append(event)
            if not progressed:
                missing = sorted(
                    {p for e in deferred for p in e.parents if p not in self._events}
                )
                raise LedgerError(
                    f"ingest cannot resolve {len(deferred)} event(s); "
                    f"missing parents: {[m[:12] for m in missing[:8]]}"
                )
            pending = deferred
        return added
