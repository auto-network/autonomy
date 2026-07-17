"""LedgerStore — the per-org SQLite replica of the authority ledger.

One SQLite file per org, alongside the org's graph DB
(``data/orgs/<slug>.ledger.db``). The store is a durability layer over
the F1 in-memory :class:`~.ledger.Ledger`: every open hydrates the full
event set (content-address-verified, anti-malleable parse) and every
append runs the complete structural verification before the row is
persisted — the disk never holds an event the in-memory ledger would
reject.

**Content addressing.** ``events.event_id`` must equal the SHA-256 of the
stored wire bytes; verified on every hydrate, so silent DB tampering is
detected at open (``TamperError``).

**L8, layer two.** Layer one is the event schema
(:func:`~.events.validate_payload` — unknown types cannot be parsed or
minted). The store re-checks the type whitelist *independently* in
:meth:`append` (catching hand-constructed Event objects that bypassed the
parser) and pins it a third time in SQL: the ``events`` table carries a
``CHECK (event_type IN (...))`` constraint, so even raw INSERTs cannot
smuggle a content-access row into the replica.

**Projections** are cache rows (``projections`` table), never the source
of truth: :meth:`rebuild_projections` drops and refolds them from the
event store, byte-identically (canonical JSON).

**Checkpoints.** ``checkpoint.state_hash`` is pinned to the fold
fingerprint at the checkpoint's parents. :func:`checkpoint_state_hash`
computes it, :meth:`verify_checkpoint` re-derives and compares, and
:meth:`cold_join` builds a fresh replica from a bundle while insisting
the named checkpoint verifies — a tampered history cannot cold-join.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .errors import LedgerError, SchemaError
from .events import EVENT_TYPES, Event
from .fold import FoldState, fold
from .ledger import Ledger
from .projections import build_projections, projection_bytes

LEDGER_SCHEMA_VERSION = 1
LEDGER_DB_SUFFIX = ".ledger.db"

_REPO_ROOT = Path(__file__).resolve().parents[3]


class StoreError(LedgerError):
    """The replica store refused an operation."""


class TamperError(StoreError):
    """Stored bytes do not match their content address / checkpoint."""


def org_ledger_db_path(slug: str, root=None) -> Path:
    """``<orgs_dir>/<slug>.ledger.db`` — alongside the org's graph DB.

    Mirrors ``tools/graph/db.py`` resolution (``AUTONOMY_ORGS_DIR`` env
    override, default ``data/orgs/``) without importing tools.graph — the
    network library stays dependency-light.
    """
    if root is not None:
        base = Path(root)
    else:
        env = os.environ.get("AUTONOMY_ORGS_DIR")
        base = Path(env) if env else _REPO_ROOT / "data" / "orgs"
    return base / f"{slug}{LEDGER_DB_SUFFIX}"


_TYPE_LIST = ", ".join(f"'{t}'" for t in sorted(EVENT_TYPES))

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    event_id   TEXT PRIMARY KEY,
    event_type TEXT NOT NULL CHECK (event_type IN ({_TYPE_LIST})),
    author_key TEXT NOT NULL,
    hlc_ts     INTEGER NOT NULL,
    hlc_count  INTEGER NOT NULL,
    wire       BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS parents (
    event_id  TEXT NOT NULL REFERENCES events(event_id),
    parent_id TEXT NOT NULL REFERENCES events(event_id),
    PRIMARY KEY (event_id, parent_id)
);
CREATE INDEX IF NOT EXISTS idx_parents_parent ON parents(parent_id);
CREATE TABLE IF NOT EXISTS heads (
    event_id TEXT PRIMARY KEY REFERENCES events(event_id)
);
CREATE TABLE IF NOT EXISTS projections (
    name        TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    body        BLOB NOT NULL
);
"""


class LedgerStore:
    """A durable, self-verifying replica. Not thread-safe (one writer)."""

    def __init__(self, path=":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self.db.execute("PRAGMA journal_mode = WAL")
        with self.db:
            self.db.executescript(_SCHEMA)
            self.db.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(LEDGER_SCHEMA_VERSION),),
            )
        self.ledger = Ledger()
        self._hydrate()

    # -- lifecycle ---------------------------------------------------------------

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "LedgerStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _hydrate(self) -> None:
        rows = self.db.execute("SELECT event_id, wire FROM events").fetchall()
        events = []
        for event_id, wire in rows:
            if hashlib.sha256(wire).hexdigest() != event_id:
                raise TamperError(
                    f"stored event {event_id[:12]} does not match its content address"
                )
            events.append(Event.from_json(bytes(wire)))  # anti-malleable parse
        if events:
            self.ledger.ingest(events)
        stored_heads = {r[0] for r in self.db.execute("SELECT event_id FROM heads")}
        if stored_heads != set(self.ledger.heads()):
            raise TamperError("stored heads table does not match the event DAG")

    # -- write side ----------------------------------------------------------------

    def append(self, event: Event) -> str:
        """Verify (structure + signature + L8) and persist one event."""
        # L8, layer two: independent of the parser — a hand-constructed
        # Event object with a non-authority payload dies here too.
        if not isinstance(event, Event) or event.payload.get("type") not in EVENT_TYPES:
            raise SchemaError(
                "store refuses non-authority event "
                f"type {getattr(event, 'payload', {}).get('type')!r} (L8)"
            )
        known = event.event_id in self.ledger
        self.ledger.add(event)  # full structural verification; idempotent
        if known:
            return event.event_id
        with self.db:
            self.db.execute(
                "INSERT INTO events(event_id, event_type, author_key, hlc_ts, hlc_count, wire)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    event.event_id,
                    event.type,
                    event.author_key,
                    event.hlc.ts,
                    event.hlc.count,
                    event.to_json(),
                ),
            )
            self.db.executemany(
                "INSERT INTO parents(event_id, parent_id) VALUES (?, ?)",
                [(event.event_id, p) for p in event.parents],
            )
            self.db.executemany(
                "DELETE FROM heads WHERE event_id = ?", [(p,) for p in event.parents]
            )
            self.db.execute("INSERT INTO heads(event_id) VALUES (?)", (event.event_id,))
        return event.event_id

    def append_wire(self, raw) -> str:
        """Parse canonical wire bytes (L8 layer one) and append."""
        return self.append(Event.from_json(raw))

    def append_bundle(self, events: Iterable[Event]) -> List[str]:
        """Append a batch in any order (sync-bundle semantics)."""
        pending = list(events)
        added: List[str] = []
        while pending:
            progressed = False
            deferred: List[Event] = []
            for event in pending:
                ready = event.type == "genesis" or (
                    self.ledger.genesis_id is not None
                    and all(p in self.ledger for p in event.parents)
                )
                if ready:
                    added.append(self.append(event))
                    progressed = True
                else:
                    deferred.append(event)
            if not progressed:
                raise LedgerError(
                    f"bundle cannot resolve {len(deferred)} event(s): missing parents"
                )
            pending = deferred
        return added

    # -- read side --------------------------------------------------------------------

    def __len__(self) -> int:
        return len(self.ledger)

    def __contains__(self, event_id: str) -> bool:
        return event_id in self.ledger

    def get(self, event_id: str) -> Event:
        return self.ledger.get(event_id)

    def heads(self) -> tuple:
        return self.ledger.heads()

    def events(self) -> List[Event]:
        return self.ledger.events()

    def fold(self, heads=None, now: Optional[int] = None) -> FoldState:
        return fold(self.ledger, heads=heads, now=now)

    # -- projections (derived cache; ops are the primitive) -----------------------------

    def refresh_projections(self, now: Optional[int] = None) -> Dict[str, bytes]:
        """Fold, build all read models, persist, return canonical bytes."""
        state = self.fold(now=now)
        rendered = {
            name: projection_bytes(p) for name, p in build_projections(state).items()
        }
        fingerprint = state.fingerprint()
        with self.db:
            self.db.execute("DELETE FROM projections")
            self.db.executemany(
                "INSERT INTO projections(name, fingerprint, body) VALUES (?, ?, ?)",
                [(name, fingerprint, body) for name, body in rendered.items()],
            )
        return rendered

    def load_projections(self) -> Dict[str, bytes]:
        return {
            name: bytes(body)
            for name, body in self.db.execute("SELECT name, body FROM projections")
        }

    def rebuild_projections(self, now: Optional[int] = None) -> Dict[str, bytes]:
        """Drop every cached read model and refold from the event store."""
        with self.db:
            self.db.execute("DELETE FROM projections")
        return self.refresh_projections(now=now)

    # -- checkpoints ---------------------------------------------------------------------

    def checkpoint_state_hash(self, parents: Iterable[str]) -> str:
        """The state_hash a checkpoint with these parents must carry."""
        return checkpoint_state_hash(self.ledger, parents)

    def verify_checkpoint(self, checkpoint_id: str) -> bool:
        """Re-derive the fold at the checkpoint's parents and compare."""
        event = self.ledger.get(checkpoint_id)
        if event.type != "checkpoint":
            raise StoreError(f"{checkpoint_id[:12]} is not a checkpoint event")
        return event.payload["state_hash"] == self.checkpoint_state_hash(event.parents)

    @classmethod
    def cold_join(
        cls, path, bundle: Iterable[Event], checkpoint_id: str
    ) -> "LedgerStore":
        """Bootstrap a fresh replica from a bundle, gated on a checkpoint.

        The bundle must contain the checkpoint's full ancestry (history is
        retained — spec §2); the checkpoint's ``state_hash`` must match the
        re-derived fold at its parents, proving the received history is the
        one the checkpoint signers folded. Tampered or truncated bundles
        raise :class:`TamperError` / :class:`LedgerError`.
        """
        store = cls(path)
        if len(store.ledger):
            raise StoreError("cold_join requires an empty replica")
        store.append_bundle(bundle)
        if checkpoint_id not in store.ledger:
            raise TamperError("bundle does not contain the announced checkpoint")
        if not store.verify_checkpoint(checkpoint_id):
            raise TamperError(
                f"checkpoint {checkpoint_id[:12]} state_hash does not match the "
                "fold of the received history"
            )
        store.refresh_projections()
        return store


def checkpoint_state_hash(ledger: Ledger, parents: Iterable[str]) -> str:
    """Fold fingerprint at *parents* — what ``checkpoint.state_hash`` pins.

    Defined over the checkpoint's parents (the state being attested),
    not the checkpoint itself: the checkpoint event cannot include its
    own hash in the state it signs.
    """
    return fold(ledger, heads=list(parents)).fingerprint()
