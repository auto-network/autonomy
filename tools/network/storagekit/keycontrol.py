"""KeyControlStore — the per-organization store for signed key-control records.

``1e005d5c-c11`` §1b names this store: the per-org home for
``KeyControlRecord``s (the union of ``StorageStateDescriptor``,
``ParentBridge``, ``CapabilityGrant``, ``CapabilityReceipt`` and
``PersonaKemCredential``), analogous to ``store.ledger`` but for the
storage side. Contract ``bb971a32-ed1`` §1 places these records OUTSIDE
the authority vocabulary — they are verified against the authority fold
but are not ledger events, so they never enter ``EVENT_TYPES``.

**Where it lives.** The same file the ledger already co-locates in: the
org's OWN ``data/orgs/<slug>.db`` (``org_ledger_db_path``), under a
``keycontrol_`` table prefix, beside the ``ledger_`` tables and the graph
tables. A separate ``keycontrol.db`` is NOT created — a separate ledger
file of exactly this kind was built, judged wrong, and migrated away from
(``relocate_ledger_to_org_db``). The store opens its own WAL-mode
connection so it coexists with the ledger and graph connections.

**This module — StorageStateDescriptor only.** The first of five
record-type sub-beads. It persists ``StorageStateDescriptor`` records
keyed by ``state_id`` and exposes the ``ancestry(heads)`` traversal over
their ``parent_state_ids`` edges (register pin 4). The other four record
types (bridges, grants, receipts, credentials) are added by later
sub-beads.

**Content addressing.** ``keycontrol_state.state_id`` must equal the
SHA-256 of the stored signed wire (``record_id``); it is verified on
every hydrate, so a tampered or mis-keyed row is caught at open
(:class:`TamperError`), exactly as the ledger store guards its events.

**Acceptance.** :meth:`accept_state` runs the real
:func:`acceptance.accept_state` against the authority fold before the row
is persisted; nothing is stored when acceptance refuses, and the distinct
refusal class (``ScopeError`` / ``LossCoverageError`` / ``DomainError`` /
…) propagates unchanged so the caller learns which check failed.

Pure library: depends on ``storagekit`` siblings, ``ledger`` (for the
co-located path resolution only), and the standard library.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from tools.network.ledger.store import org_ledger_db_path

from . import acceptance
from .errors import StorageError
from .records import record_id
from .state import StorageStateDescriptor

KEYCONTROL_SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS keycontrol_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS keycontrol_state (
    state_id TEXT PRIMARY KEY,
    wire     BLOB NOT NULL
);
"""


class KeyControlStoreError(StorageError):
    """The key-control store refused an operation."""


class TamperError(KeyControlStoreError):
    """A stored record's bytes do not match its content-addressed key."""


class UnknownStateError(KeyControlStoreError):
    """Ancestry was asked about a ``state_id`` the store has never seen."""


class KeyControlStore:
    """A durable, self-verifying store of key-control records. One writer.

    Reuses ``org_ledger_db_path`` — pass a slug to target the org's own
    ``data/orgs/<slug>.db``, or an explicit path (``:memory:`` for a
    transient store). Every open hydrates the full descriptor set,
    content-address-verified, so the in-memory view can never hold a row
    the disk would reject.
    """

    def __init__(self, path=":memory:", *, slug=None, root=None):
        if slug is not None:
            path = org_ledger_db_path(slug, root=root)
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        if self.path != ":memory:":
            self.db.execute("PRAGMA journal_mode = WAL")
        with self.db:
            self.db.executescript(_SCHEMA)
            self.db.execute(
                "INSERT OR IGNORE INTO keycontrol_meta(key, value) VALUES"
                " ('schema_version', ?)",
                (str(KEYCONTROL_SCHEMA_VERSION),),
            )
        self._states: dict = {}  # state_id -> StorageStateDescriptor
        self._hydrate()

    # -- lifecycle -------------------------------------------------------------------

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "KeyControlStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _hydrate(self) -> None:
        rows = self.db.execute("SELECT state_id, wire FROM keycontrol_state").fetchall()
        for state_id, wire in rows:
            wire = bytes(wire)
            if record_id(wire) != state_id:
                raise TamperError(
                    f"stored descriptor {state_id[:12]} does not match its "
                    "content address"
                )
            # Anti-malleable strict parse + full structural verification; the
            # descriptor's own state_id equals record_id(wire) by construction.
            descriptor = StorageStateDescriptor.from_json(wire)
            self._states[state_id] = descriptor

    # -- write side ------------------------------------------------------------------

    def accept_state(
        self,
        descriptor: StorageStateDescriptor,
        fold_at,
        authority_ancestry,
        *,
        bridges=(),
        parent_descriptors=None,
    ) -> acceptance.AcceptedState:
        """Verify *descriptor* against the authority fold, then persist it.

        ``fold_at`` and ``authority_ancestry`` are the AUTHORITY-ledger
        seams :func:`acceptance.accept_state` folds at the record's cited
        frontier with (distinct from this store's storage-DAG
        :meth:`ancestry`). Acceptance runs FIRST: on any refusal its
        distinct :class:`~.acceptance.AcceptanceError` subclass propagates
        unchanged and no row is written. ``history_complete`` is computed
        by acceptance and returned, never stored — its inputs
        (``ParentBridge`` records) arrive after the descriptor and belong
        to a later sub-bead.
        """
        result = acceptance.accept_state(
            descriptor,
            fold_at,
            authority_ancestry,
            bridges=bridges,
            parent_descriptors=parent_descriptors,
        )
        self._put(descriptor)
        return result

    def _put(self, descriptor: StorageStateDescriptor) -> None:
        wire = descriptor.to_json()
        state_id = descriptor.state_id
        if record_id(wire) != state_id:  # invariant guard; true for a real record
            raise TamperError("descriptor state_id does not equal record_id(wire)")
        existing = self._states.get(state_id)
        if existing is not None:
            # Content-addressed dedupe (§1e): an identical derivation is the
            # identical record. Same key ⇒ same wire (SHA-256), so re-storing
            # is idempotent and writes no second row.
            return
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO keycontrol_state(state_id, wire) VALUES (?, ?)",
                (state_id, wire),
            )
        self._states[state_id] = descriptor

    # -- read side -------------------------------------------------------------------

    def get(self, state_id: str):
        """The stored :class:`StorageStateDescriptor`, or ``None``."""
        return self._states.get(state_id)

    def __contains__(self, state_id: str) -> bool:
        return state_id in self._states

    def __len__(self) -> int:
        return len(self._states)

    @property
    def states(self) -> dict:
        """A snapshot copy of ``state_id -> descriptor``."""
        return dict(self._states)

    def ancestry(self, heads) -> frozenset:
        """Inclusive closure over ``parent_state_ids`` edges (register pin 4).

        Named as ``1e005d5c-c11`` §1b names it. The result INCLUDES the
        identifiers passed in (``ancestry(x) ∋ x``), matching
        :meth:`Ledger.ancestry`'s inclusive closure that ``dominates`` is
        written against. Interior ancestors named by a stored descriptor's
        signed ``parent_state_ids`` are members even if their own
        descriptor has not yet been stored — the edge is signed provenance
        — but an INPUT identifier the store has never seen is REFUSED,
        matching ``dominates`` (``witness.py:112-117``), which fails closed
        on an unknown id rather than let a not-yet-synced replica evaluate
        a retraction of heads it cannot see.
        """
        heads = list(heads)
        for head in heads:
            if head not in self._states:
                raise UnknownStateError(
                    f"ancestry asked about unseen state {head[:12]!r}: sync the "
                    "descriptor before evaluating it"
                )
        seen: set = set()
        stack = list(heads)
        while stack:
            state_id = stack.pop()
            if state_id in seen:
                continue
            seen.add(state_id)
            descriptor = self._states.get(state_id)
            if descriptor is not None:
                stack.extend(descriptor.parent_state_ids)
        return frozenset(seen)
