"""LedgerStore — the org-DB-co-located SQLite replica of the ledger.

One organization is one database file: the ledger tables live inside
the org's own ``data/orgs/<slug>.db`` under a ``ledger_`` name prefix,
beside the graph tables (which track their schema via ``PRAGMA
user_version``; the ledger keeps its own ``ledger_meta`` version row).
The store opens its own WAL-mode connection, so it coexists with the
graph connection on the same file, and stays dependency-light — it
never imports tools.graph. The store is a durability layer over the F1
in-memory :class:`~.ledger.Ledger`: every open hydrates the full event
set (content-address-verified, anti-malleable parse) and every append
runs the complete structural verification before the row is persisted —
the disk never holds an event the in-memory ledger would reject.
Pre-co-location ``<slug>.ledger.db`` files migrate through
:func:`relocate_ledger_to_org_db`.

**Content addressing.** ``ledger_events.event_id`` must equal the
SHA-256 of the stored wire bytes; verified on every hydrate, so silent
DB tampering is detected at open (``TamperError``).

**L8, layer two.** Layer one is the event schema
(:func:`~.events.validate_payload` — unknown types cannot be parsed or
minted). The store re-checks the type whitelist *independently* in
:meth:`append` (catching hand-constructed Event objects that bypassed the
parser) and pins it a third time in SQL: the ``ledger_events`` table
carries a ``CHECK (event_type IN (...))`` constraint, so even raw
INSERTs cannot smuggle a content-access row into the replica.

**Projections** are cache rows (``ledger_projections``), never the source
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
import json
import logging
import sqlite3
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from tools.data_paths import DATA_ROOT, resolve_orgs_root
from tools.network import clock
from tools.network.idkit import canonical_json, verify_signature
from tools.network.idkit.errors import IdkitError

from .errors import LedgerError, SchemaError, SignatureError
from .events import EVENT_TYPES, Event, approval_signing_input
from .fold import (
    R_APPROVAL_MISSING,
    R_ROLE_UNDEFINED,
    FoldState,
    admitting_approvers,
    claim_requirement_status,
    fold,
)
from .ledger import Ledger
from .projections import build_projections, projection_bytes

LEDGER_SCHEMA_VERSION = 1
LEDGER_DB_SUFFIX = ".ledger.db"

#: How long a staged claim stays finalizable, measured from STAGING
#: (auto-cz4fb). Pinned-position finalize means the invite's own expiry
#: no longer bounds when an admission can land, so the staging row needs
#: its own bound or an admission could be resurrected indefinitely from
#: stale staging state. Server wall clock, not the client's HLC. Owned by
#: tools.network.clock (an expiry-sweep gate); re-exported here for the
#: staging call sites that always read it from this module.
from tools.network.clock import PENDING_CLAIM_TTL_MS

_REPO_ROOT = Path(__file__).resolve().parents[3]


class StoreError(LedgerError):
    """The replica store refused an operation."""


class TamperError(StoreError):
    """Stored bytes do not match their content address / checkpoint."""


def _orgs_dir(root=None) -> Path:
    return resolve_orgs_root(root, default=DATA_ROOT / "orgs")


def org_ledger_db_path(slug: str, root=None) -> Path:
    """``<orgs_dir>/<slug>.db`` — the org's OWN database; the ledger
    tables live inside it under the ``ledger_`` prefix.

    Mirrors ``tools/graph/db.py`` resolution (``AUTONOMY_ORGS_DIR`` env
    override, default ``data/orgs/``) without importing tools.graph — the
    network library stays dependency-light. The legacy separate file is
    ``<slug>{LEDGER_DB_SUFFIX}``; see :func:`relocate_ledger_to_org_db`.

    The two LOCAL stores are not organizations and live beside the orgs
    directory (auto-35kmy); this resolver must agree with the graph's
    about that or the same slug names two files (the split-resolver
    failure). Both consume ``tools.data_paths.LOCAL_STORE_KEYS``. No
    local store ever holds a founded ledger — this routing exists so a
    path QUESTION about one gets the true answer rather than minting a
    stray file in the org namespace.
    """
    from tools.data_paths import LOCAL_STORE_KEYS, resolve_local_store_path

    d = _orgs_dir(root)
    if slug in LOCAL_STORE_KEYS:
        # THE shared resolver — routing and classification identical to
        # the graph side by construction, not by parallel maintenance.
        return resolve_local_store_path(slug, d)
    return d / f"{slug}.db"


_TYPE_LIST = ", ".join(f"'{t}'" for t in sorted(EVENT_TYPES))

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS ledger_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger_events (
    event_id   TEXT PRIMARY KEY,
    event_type TEXT NOT NULL CHECK (event_type IN ({_TYPE_LIST})),
    author_key TEXT NOT NULL,
    hlc_ts     INTEGER NOT NULL,
    hlc_count  INTEGER NOT NULL,
    wire       BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger_parents (
    event_id  TEXT NOT NULL REFERENCES ledger_events(event_id),
    parent_id TEXT NOT NULL REFERENCES ledger_events(event_id),
    PRIMARY KEY (event_id, parent_id)
);
CREATE INDEX IF NOT EXISTS idx_ledger_parents_parent ON ledger_parents(parent_id);
CREATE TABLE IF NOT EXISTS ledger_heads (
    event_id TEXT PRIMARY KEY REFERENCES ledger_events(event_id)
);
CREATE TABLE IF NOT EXISTS ledger_projections (
    name        TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    body        BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger_pending_claims (
    claim_key   TEXT PRIMARY KEY,
    invite_ref  TEXT NOT NULL,
    persona_pub TEXT NOT NULL,
    body        BLOB NOT NULL,
    approvals   BLOB NOT NULL,
    author_key  TEXT NOT NULL,
    hlc_ts      INTEGER NOT NULL,
    -- The claim's FIXED causal position (auto-cz4fb): finalization
    -- re-mints here, not at the current frontier, so a claim that
    -- entered before the invite expired stays admittable however long
    -- approval takes. hlc_count is as load-bearing as hlc_ts.
    parents     BLOB,
    hlc_count   INTEGER,
    -- Server wall clock at staging: the pending-claim TTL's origin
    -- (the client-supplied hlc is not a trustworthy clock).
    staged_at   INTEGER
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
                "INSERT OR IGNORE INTO ledger_meta(key, value) VALUES ('schema_version', ?)",
                (str(LEDGER_SCHEMA_VERSION),),
            )
        self._migrate_pending_claim_position()
        self.ledger = Ledger()
        self._hydrate()

    # -- lifecycle ---------------------------------------------------------------

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "LedgerStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _migrate_pending_claim_position(self) -> None:
        """Add the fixed-position columns to a pre-cz4fb staging table.

        Idempotent. A row staged before this migration carries no
        parents/hlc_count and therefore cannot be pinned-finalized — it
        surfaces as ``legacy-staging`` so the invitee re-submits, rather
        than silently finalizing at the wrong causal position.
        """
        existing = {
            row[1]
            for row in self.db.execute("PRAGMA table_info(ledger_pending_claims)")
        }
        with self.db:
            for column, decl in (
                ("parents", "BLOB"),
                ("hlc_count", "INTEGER"),
                ("staged_at", "INTEGER"),
            ):
                if column not in existing:
                    self.db.execute(
                        f"ALTER TABLE ledger_pending_claims ADD COLUMN {column} {decl}"
                    )

    def _hydrate(self) -> None:
        """Load this store's events from their Settings rows.

        Events ARE Settings rows (design graph://53b5bb04-bc0): one home, and
        replication delivers a co-member's events into the same place this
        reads from. A legacy store's ``ledger_events`` table is carried across
        first; heads are computed from the graph rather than stored, since the
        parents are inside each signed event.
        """
        from .settings_bridge import (
            ensure_settings_table, migrate_events_to_settings, read_event_wires,
        )

        ensure_settings_table(self.db)
        migrate_events_to_settings(self.db, self.path, label=Path(self.path).stem)
        wires = read_event_wires(self.db)
        events = []
        for event_id, wire in wires.items():
            raw = wire.encode("utf-8")
            if hashlib.sha256(raw).hexdigest() != event_id:
                raise TamperError(
                    f"stored event {event_id[:12]} does not match its content address"
                )
            events.append(Event.from_json(raw))  # anti-malleable parse
        if events:
            self.ledger.ingest(events)

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
        # ONE write, to the one home. The Settings row IS the event's storage
        # (design graph://53b5bb04-bc0), and that write is what the capture
        # triggers replicate -- so an event is on the wire by virtue of being
        # stored. A failure here is a failure to record the event and is
        # raised, not swallowed: there is no second copy to fall back on and
        # nothing to reconcile later.
        from .settings_bridge import write_event

        write_event(self.path, event.event_id, event.to_json().decode("utf-8"))
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

    # -- pending claims (staging rows, NEVER events rows — auto-g6q9d) -----------------

    def evaluate_claim(self, event: Event) -> Optional[str]:
        """Trial-fold a ``member.claim`` against the current DAG.

        Nothing is persisted: the candidate is verified and folded in a
        scratch ledger. Returns ``None`` (folds clean — append it),
        ``"approval-missing"`` (stage it pending), or the hard rejection
        reason. Structural/signature defects raise exactly as ``append``
        would.
        """
        if not isinstance(event, Event) or event.type != "member.claim":
            raise SchemaError("evaluate_claim takes a member.claim event")
        scratch = Ledger()
        scratch.ingest(self.ledger.events())
        scratch.add(event)  # full structural + signature verification
        state = fold(scratch)
        if state.valid[event.event_id]:
            return None
        return state.reasons[event.event_id]

    @staticmethod
    def claim_key(invite_ref: str, persona_pub: str) -> str:
        return hashlib.sha256((invite_ref + persona_pub).encode("ascii")).hexdigest()

    def stage_pending_claim(self, event: Event, *, now: Optional[int] = None) -> str:
        """Hold an under-approved claim for countersignatures.

        A staging row only — the event tables, content-address hydrate,
        and L8 CHECK are untouched. Idempotent per (invite, persona).
        """
        if not isinstance(event, Event) or event.type != "member.claim":
            raise SchemaError("stage_pending_claim takes a member.claim event")
        p = event.payload
        key = self.claim_key(p["invite_ref"], p["persona_pub"])
        staged_at = clock.now_ms(now)
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO ledger_pending_claims VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    key,
                    p["invite_ref"],
                    p["persona_pub"],
                    canonical_json(p),
                    canonical_json(list(p["approvals"])),
                    event.author_key,
                    event.hlc.ts,
                    # The fixed causal position finalization re-mints at.
                    canonical_json(list(event.parents)),
                    event.hlc.count,
                    staged_at,
                ),
            )
        return key

    def get_pending_claim(self, claim_key: str) -> Optional[dict]:
        row = self.db.execute(
            "SELECT invite_ref, persona_pub, body, approvals, author_key, hlc_ts, "
            "parents, hlc_count, staged_at "
            "FROM ledger_pending_claims WHERE claim_key = ?",
            (claim_key,),
        ).fetchone()
        if row is None:
            return None
        return {
            "claim_key": claim_key,
            "invite_ref": row[0],
            "persona_pub": row[1],
            "body": json.loads(bytes(row[2])),
            "approvals": json.loads(bytes(row[3])),
            "author_key": row[4],
            "hlc_ts": row[5],
            # The fixed causal position (auto-cz4fb). None on a row staged
            # before the migration: it cannot be pinned-finalized, so the
            # readiness seam reports legacy-staging and the invitee
            # re-submits rather than finalizing at the wrong position.
            "parents": json.loads(bytes(row[6])) if row[6] is not None else None,
            "hlc_count": row[7],
            "staged_at": row[8],
        }

    def list_pending_claims(self) -> list:
        """Every staged claim, oldest first — the membership surface's
        pending-request list. Same row shape as :meth:`get_pending_claim`."""
        keys = [
            row[0] for row in self.db.execute(
                "SELECT claim_key FROM ledger_pending_claims "
                "ORDER BY staged_at, claim_key"
            )
        ]
        return [self.get_pending_claim(key) for key in keys]

    def add_pending_approval(self, claim_key: str, entry: dict) -> list:
        """Merge one verified ``{key, sig}`` countersignature.

        The store verifies the SIGNATURE over the staged body (fail
        closed — an unverifiable entry never lands); whether the signer
        holds admission AUTHORITY is the caller's fold-side check.
        Returns the merged, key-sorted, duplicate-free approvals list.
        """
        record = self.get_pending_claim(claim_key)
        if record is None:
            raise StoreError(f"no pending claim {claim_key[:12]}")
        if not isinstance(entry, dict) or set(entry) != {"key", "sig"}:
            raise SchemaError("approval entry must be exactly {key, sig}")
        try:
            verify_signature(
                entry["key"],
                entry["sig"],
                approval_signing_input("member.claim", record["body"]),
            )
        except IdkitError as exc:
            raise SignatureError(
                "countersignature does not verify over the staged claim"
            ) from exc
        merged = {e["key"]: e for e in record["approvals"]}
        merged[entry["key"]] = {"key": entry["key"], "sig": entry["sig"]}
        approvals = [merged[k] for k in sorted(merged)]
        with self.db:
            self.db.execute(
                "UPDATE ledger_pending_claims SET approvals = ? WHERE claim_key = ?",
                (canonical_json(approvals), claim_key),
            )
        return approvals

    def drop_pending_claim(self, claim_key: str) -> None:
        with self.db:
            self.db.execute(
                "DELETE FROM ledger_pending_claims WHERE claim_key = ?", (claim_key,)
            )

    def evaluate_pending_claim(self, claim_key: str, *, now: Optional[int] = None) -> dict:
        """Readiness of a staged claim: ``{ready, have, need, reason}``.

        No invitee-signed event exists for the merged-approvals state
        (merging changes the payload the invitee signed), so readiness is
        computed WITHOUT one: every merged countersignature is
        RE-VALIDATED over the staged body (fail closed), the invite and
        role resolve through the current fold (authority judged at the
        stored claim ts, matching what ``_check_approvals`` will apply),
        and ``have``/``need`` come from the SAME acceptance core the fold
        uses — route-side status cannot drift from the fold's verdict.
        ``need`` is the total required count (the "N of M" view). The
        final append remains authoritative: finalization re-mints under
        the invitee's key and runs the full fold.
        """
        record = self.get_pending_claim(claim_key)
        if record is None:
            raise StoreError(f"no pending claim {claim_key[:12]}")
        body = record["body"]
        signing_input = approval_signing_input("member.claim", body)
        for entry in record["approvals"]:
            try:
                verify_signature(entry["key"], entry["sig"], signing_input)
            except IdkitError as exc:
                raise SignatureError(
                    "a staged countersignature no longer verifies"
                ) from exc
        invite_event = self.get(record["invite_ref"])
        role = invite_event.payload["granted_role"]
        sponsor = invite_event.payload["sponsor"]
        approver_keys = [e["key"] for e in record["approvals"]]
        state = self.fold(now=record["hlc_ts"])
        view = state.role_defs.get(role)
        if view is None:
            return {
                "ready": False, "have": 0, "need": 0,
                "reason": R_ROLE_UNDEFINED, "admitting": [], "position": None,
            }
        have, need = claim_requirement_status(
            requires=view.claim_requires,
            key_bound="invite_pub" in invite_event.payload,
            approver_keys=approver_keys,
            threshold=view.approver_threshold,
            root=state.root,
            sponsor=sponsor,
            role=role,
            holds=state.holds,
        )
        expired = (
            record["staged_at"] is not None
            and clock.now_ms(now)
            > record["staged_at"] + PENDING_CLAIM_TTL_MS
        )
        if expired:
            # The claim entered validly but its staging window closed: a
            # distinct terminal state, never a silent pending.
            return {
                "ready": False, "have": have, "need": need,
                "reason": "claim-expired", "admitting": [],
                "position": None,
            }
        if record["parents"] is None:
            # Pre-migration staging row: no causal position to re-mint at.
            return {
                "ready": False, "have": have, "need": need,
                "reason": "legacy-staging", "admitting": [], "position": None,
            }
        ready = have >= need
        # ``admitting``: a deterministic NEED-sized subset of the approvers
        # that count (sorted-by-key first ``need``) — finalization re-mints
        # with EXACTLY this subset, so the final payload stays within
        # MAX_APPROVALS no matter how many authorized approvers signed.
        # The verdict above is claim_requirement_status's alone; this only
        # exposes the set it consulted.
        if need == 0:
            admitting: list = []
        elif view.claim_requires == "sponsor":
            admitting = [sponsor] if sponsor in approver_keys else []
        else:
            counted = admitting_approvers(
                approver_keys,
                root=state.root, sponsor=sponsor, role=role, holds=state.holds,
            )
            admitting = sorted(counted)[:need]
        return {
            "ready": ready,
            "have": have,
            "need": need,
            "reason": None if ready else R_APPROVAL_MISSING,
            "admitting": admitting,
            # Where finalization must re-mint (auto-cz4fb): the claim's
            # ORIGINAL causal position, so a pre-expiry claim stays
            # admittable however long approval took.
            "position": {
                "parents": list(record["parents"]),
                "hlc": [record["hlc_ts"], record["hlc_count"]],
            },
        }

    # -- read side --------------------------------------------------------------------

    def __len__(self) -> int:
        return len(self.ledger)

    def __contains__(self, event_id: str) -> bool:
        return event_id in self.ledger

    def get(self, event_id: str) -> Event:
        return self.ledger.get(event_id)

    def heads(self) -> tuple:
        return self.ledger.heads()

    @property
    def genesis_id(self):
        """The store-compatible read the in-memory ledger already exposes, so a
        writer takes one interface — ``append`` / ``genesis_id`` / ``heads`` —
        over either a store or a bare ledger."""
        return self.ledger.genesis_id

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
            self.db.execute("DELETE FROM ledger_projections")
            self.db.executemany(
                "INSERT INTO ledger_projections(name, fingerprint, body) VALUES (?, ?, ?)",
                [(name, fingerprint, body) for name, body in rendered.items()],
            )
        return rendered

    def load_projections(self) -> Dict[str, bytes]:
        return {
            name: bytes(body)
            for name, body in self.db.execute("SELECT name, body FROM ledger_projections")
        }

    def rebuild_projections(self, now: Optional[int] = None) -> Dict[str, bytes]:
        """Drop every cached read model and refold from the event store."""
        with self.db:
            self.db.execute("DELETE FROM ledger_projections")
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


def relocate_ledger_to_org_db(slug: str, *, root=None) -> bool:
    """Move a legacy ``<slug>.ledger.db`` into the co-located org DB.

    Every relocated event is read from a legacy connection, re-passes
    content-address verification here and full structural verification
    in :meth:`LedgerStore.append`, and the migrated store's fold
    fingerprint and every rendered projection must equal the legacy
    store's byte-for-byte before the legacy file is renamed to
    ``<slug>.ledger.db.migrated``. Idempotent: an absent or empty legacy
    file returns ``False`` and changes nothing.
    """
    legacy_path = _orgs_dir(root) / f"{slug}{LEDGER_DB_SUFFIX}"
    if not legacy_path.is_file():
        return False
    legacy_db = sqlite3.connect(legacy_path)
    try:
        # A legacy file may hold its events in any of the three shapes this
        # store has had: the original `events` table, the interim
        # `ledger_events` table, or -- for a file written after the
        # conversion -- the Settings rows that are now the only storage.
        rows: list = []
        for statement in (
            "SELECT event_id, wire FROM events",
            "SELECT event_id, wire FROM ledger_events",
        ):
            try:
                rows = legacy_db.execute(statement).fetchall()
            except sqlite3.OperationalError:
                continue
            if rows:
                break
        if not rows:
            from .settings_bridge import read_event_wires

            rows = [
                (event_id, wire.encode("utf-8"))
                for event_id, wire in read_event_wires(legacy_db).items()
            ]
    finally:
        legacy_db.close()
    if not rows:
        return False

    events = []
    for event_id, wire in rows:
        if hashlib.sha256(wire).hexdigest() != event_id:
            raise TamperError(
                f"legacy event {event_id[:12]} does not match its content address"
            )
        events.append(Event.from_json(bytes(wire)))
    legacy_ledger = Ledger()
    legacy_ledger.ingest(events)
    legacy_state = fold(legacy_ledger)
    legacy_rendered = {
        name: projection_bytes(p)
        for name, p in build_projections(legacy_state).items()
    }

    store = LedgerStore(org_ledger_db_path(slug, root))
    try:
        store.append_bundle(events)  # full re-verification per event
        rendered = store.refresh_projections()
        if store.fold().fingerprint() != legacy_state.fingerprint():
            raise StoreError(
                f"relocated fold fingerprint diverges for {slug!r}; migration aborted"
            )
        if rendered != legacy_rendered:
            raise StoreError(
                f"relocated projections diverge for {slug!r}; migration aborted"
            )
    finally:
        store.close()
    legacy_path.rename(Path(str(legacy_path) + ".migrated"))
    return True
