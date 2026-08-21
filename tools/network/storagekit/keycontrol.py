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

**This module.** ``StorageStateDescriptor`` records, keyed by
``state_id``, with the ``ancestry(heads)`` traversal over their
``parent_state_ids`` edges (register pin 4); ``PersonaKemCredential``
records, keyed by ``kem_key_id`` with a secondary ``persona`` index; and
``ParentBridge`` records, keyed by ``bridge_id`` with a
``(child_state_id, parent_state_id)`` edge index. The remaining record
types (grants, receipts) are added by other sub-beads.

**Bridges — persistence and indexing, no new cryptography.** A bridge
carries a PARENT state's secret encrypted under a key derived from the
CHILD's secret (``bb971a32-ed1`` §7); ``bridge.py`` owns ``create``,
``open``, ``verify_signature`` and ``recover_ancestors``, and this module
calls them unchanged. A bridge's authorization IS its descriptor's
authorization (§8, crib ``1e005d5c-c11`` §7 PIN 6b): the advance scope is
checked once, on the descriptor, so the store runs NO independent fold
check on a bridge and never resolves ``issuer_persona`` against the
roster itself. What it does check is that the bridge belongs to the one
advance operation the descriptor's acceptance already authorized — the
edge is in the child's SIGNED ``parent_state_ids``, and the bridge agrees
with the held descriptor on ``issuer_persona``, ``authority_heads``,
``genesis_id`` and ``domain_id`` (exactly the equalities
``lifecycle._mint`` produces).

**Poisoned bodies are discovered at recovery, by construction.** The
store holds ``secret_commitment`` and never state secrets, so it cannot
derive ``edge_key`` and cannot authenticate a bridge's AEAD at admission.
A bridge is persisted on signature and edge validity alone; a body that
does not open, or opens to a secret failing its parent's commitment,
raises inside :meth:`recover_ancestors` in the hands of a holder.

**``history_complete`` is recomputed, never stored.** Acceptance derives
it from the bridge set present AT EVALUATION TIME
(``acceptance.py:194-197``) and that set is not monotonic in either
direction: ``_has_one_valid_bridge`` returns ``valid == 1``, and crib §19
prunes bodies at the tail so local availability shrinks. A stored value
would be wrong the moment either happens. The edge index makes
recomputation cheap; no column stores it.

**Arrival — dependency deferral.** A bridge may arrive before the
descriptor it names. That is the shipped design (§3 makes arbitrary
delivery ordering the verification target; the ``tests/conftest.py``
harness drives shuffled two-stream feeds with deferral and asserts zero
rejects and zero undelivered). A bridge naming a state the store has not
seen is DEFERRED and retried when that state arrives — never refused, and
never resubmitted by the sender. Terminal cryptographic failures still
raise.

**Deferred records are visible and countable.** This is the first place a
deferred record accumulates durably, so it is observable from the moment
it exists: :class:`PendingRecordInfo`, :meth:`pending_count`,
:meth:`pending_records`, :meth:`pending_usage`. Counting and exposing
only — no judgement. A pending record is either legitimately early or
deliberately unresolvable and the store cannot tell them apart; deciding
which is a trust-and-safety problem for later. Nothing is evicted
automatically; removal is an explicit operator action
(:meth:`drop_pending`).

**Credentials — persistence, not selection.** :meth:`accept_credential`
runs the real :func:`credentials.validate` — Ed25519 signature over
``signing_input()``, exact ``_FIELDS`` closure, version, suite, hex
widths, and ``compute_kem_key_id(binding_dict()) == kem_key_id`` — and
refuses a failing credential TERMINALLY, storing no row. It does NOT call
``verify_against_fold``: currency is TIME-VARYING and is the reader's
question (``credentials.py:245``), so a credential valid at write must not
be retroactively invalidated by later membership. Retrieval by
``kem_key_id`` returns the one credential; retrieval by ``persona``
returns every credential that persona has published — currency is not
uniqueness in storage, because ``select_current_credential`` needs the
whole candidate set. Selection stays the caller's, run with the caller's
LEDGER ancestry (``Ledger.ancestry``, closing over ``authority_heads``) —
never this store's :meth:`ancestry`, which closes over storage
``state_id``s and is a different DAG over a different identifier space.

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

from tools.network.dag_tag import STORAGE, tag_dag

import enum
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from tools.network.fleet_sync_connection import FleetSyncConnection

from tools.network.ledger.store import org_ledger_db_path

from . import acceptance
from . import bridge as bridge_mod
from . import credentials
from .bridge import ParentBridge
from .credentials import PersonaKemCredential
from .errors import MalformedRecordError, StorageError
from .records import record_id
from .state import StorageStateDescriptor

KEYCONTROL_SCHEMA_VERSION = 2

#: Pending-store bounds. PICKED, NOT DERIVED — recorded so nothing is
#: blocked on a constant, and taken as canonical by ``auto-pw9bs.3``
#: (follow transport if they ever diverge). A key-control record is a few
#: hundred bytes, so the row bound is roughly 50 MB: a very large
#: legitimate backlog and trivial disk. Row and byte bounds are
#: independent; whichever hits first governs. Byte accounting is the
#: literal sum of persisted canonical signed-wire lengths, not a claim
#: about page or index overhead.
MAX_PENDING_ROWS = 100_000
MAX_PENDING_BYTES = 1024 * 1024 * 1024  # 1 GiB
#: Per-batch bounds. No batch entry point exists in this store — transport
#: owns framing and is where these are enforced; they are named here so the
#: two layers quote one set of numbers.
MAX_BATCH_RECORDS = 1_000
MAX_BATCH_BYTES = 16 * 1024 * 1024  # 16 MiB

#: ``PendingRecordInfo.record_type`` for the bridge family. The pending
#: store is cross-family by intent; bridges are the family this bead
#: implements.
RECORD_TYPE_BRIDGE = "bridge"
#: ``unmet_dependency_kind`` for "a storage-state descriptor I do not hold".
DEPENDENCY_STATE = "state"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS keycontrol_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS keycontrol_state (
    state_id TEXT PRIMARY KEY,
    wire     BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS keycontrol_grant (
    grant_id             TEXT PRIMARY KEY,
    storage_state_id     TEXT NOT NULL,
    recipient_kem_key_id TEXT NOT NULL,
    wire                 BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS keycontrol_grant_state
    ON keycontrol_grant (storage_state_id);
CREATE TABLE IF NOT EXISTS keycontrol_credential (
    kem_key_id TEXT PRIMARY KEY,
    persona    TEXT NOT NULL,
    -- NULL is a local tail-body prune. Fleet reconciliation restores a body
    -- from any peer that still holds it; a prune never erases a remote body.
    wire       BLOB
);
CREATE INDEX IF NOT EXISTS keycontrol_credential_persona
    ON keycontrol_credential (persona);
-- ``wire`` is NULLABLE: crib §19 prunes bodies at the tail, and the row
-- survives as the audit anchor and as the (child, parent) re-fetch key.
CREATE TABLE IF NOT EXISTS keycontrol_bridge (
    bridge_id       TEXT PRIMARY KEY,
    child_state_id  TEXT NOT NULL,
    parent_state_id TEXT NOT NULL,
    wire            BLOB
);
CREATE INDEX IF NOT EXISTS keycontrol_bridge_edge
    ON keycontrol_bridge (child_state_id, parent_state_id);
-- A grant delivers one generation's secret to one recipient, sealed to their
-- KEM credential. It is the DURABLE recovery material: at unlock the operator
-- re-derives their KEM key from the root and opens the grant to recover the
-- generation key. ``wire`` is NOT NULL — unlike a bridge/credential body a
-- grant is never pruned, because pruning it would make the generation it
-- carries unrecoverable.
CREATE TABLE IF NOT EXISTS keycontrol_grant (
    grant_id             TEXT PRIMARY KEY,
    storage_state_id     TEXT NOT NULL,
    recipient_kem_key_id TEXT NOT NULL,
    wire                 BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS keycontrol_grant_state
    ON keycontrol_grant (storage_state_id);
CREATE TABLE IF NOT EXISTS keycontrol_pending (
    record_type            TEXT NOT NULL,
    claimed_id             TEXT NOT NULL,
    unmet_dependency_kind  TEXT NOT NULL,
    unmet_dependency_id    TEXT NOT NULL,
    first_held_at_ms       INTEGER NOT NULL,
    first_delivery_peer_id TEXT,
    wire                   BLOB NOT NULL,
    wire_len               INTEGER NOT NULL,
    PRIMARY KEY (record_type, claimed_id)
);
CREATE INDEX IF NOT EXISTS keycontrol_pending_dependency
    ON keycontrol_pending (unmet_dependency_kind, unmet_dependency_id);
CREATE TABLE IF NOT EXISTS keycontrol_pending_usage (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    pending_rows  INTEGER NOT NULL,
    pending_bytes INTEGER NOT NULL
);
"""

# Capacity check and insert in ONE statement: the usage counters are read
# inside the same transaction the row is written in, so two writers racing
# for the last slot serialize on the write lock and exactly one wins.
_INSERT_PENDING_GUARDED = """
INSERT INTO keycontrol_pending
    (record_type, claimed_id, unmet_dependency_kind, unmet_dependency_id,
     first_held_at_ms, first_delivery_peer_id, wire, wire_len)
SELECT ?, ?, ?, ?, ?, ?, ?, ?
WHERE (SELECT pending_rows  FROM keycontrol_pending_usage WHERE id = 1) + 1 <= ?
  AND (SELECT pending_bytes FROM keycontrol_pending_usage WHERE id = 1) + ? <= ?
"""


def _now_ms() -> int:
    """Local receipt time, milliseconds. NOT an HLC — see
    :attr:`PendingRecordInfo.first_held_at_ms`."""
    return time.time_ns() // 1_000_000


class KeyControlStoreError(StorageError):
    """The key-control store refused an operation."""


class TamperError(KeyControlStoreError):
    """A stored record's bytes do not match its content-addressed key."""


class UnknownStateError(KeyControlStoreError):
    """Ancestry was asked about a ``state_id`` the store has never seen."""


class UnsignedEdgeError(KeyControlStoreError):
    """A bridge names a parent that is not in its child descriptor's SIGNED
    ``parent_state_ids``.

    Terminal, and load-bearing: :func:`bridge.recover_ancestors` groups
    bridges by ``child_state_id`` alone (``bridge.py:249-252``) and never
    consults the signed edge, so a stored ``C -> P`` would be FOLLOWED even
    where C's signed parents are ``[Q]``. Without this check the recovery
    graph diverges from the signed DAG.
    """


class BridgeContextError(KeyControlStoreError):
    """A bridge disagrees with its held descriptor on ``issuer_persona``,
    ``authority_heads``, ``genesis_id`` or ``domain_id``.

    With no independent fold check on a bridge, ``issuer_persona ==
    creator_persona`` AND ``authority_heads == descriptor.authority_heads``
    are what authenticate it as part of the one advance operation §8
    authorizes. Both are required, not just the issuer.
    """


class Admission(enum.Enum):
    """The three non-raising outcomes of offering a record.

    Four outcomes exist and they are distinct: ``ACCEPTED``, durable
    ``DEFERRED``, non-durable ``RETRY_LATER_CAPACITY``, and terminal
    refusal — which raises a :class:`~.errors.StorageError` subclass rather
    than returning.
    """

    ACCEPTED = "accepted"
    DEFERRED = "deferred"
    RETRY_LATER_CAPACITY = "retry_later_capacity"


@dataclass(frozen=True)
class PendingLimits:
    """Per-org pending bounds. Defaults are the canonical constants;
    tests inject small ones to reach the boundary without 100k rows."""

    max_rows: int = MAX_PENDING_ROWS
    max_bytes: int = MAX_PENDING_BYTES


@dataclass(frozen=True)
class PendingRecordInfo:
    """One durably-held record awaiting a dependency.

    Cross-family by intent — ONE shape for all five key-control record
    families, not five bespoke ones — though bridges are the only family
    that populates it today.

    ``first_held_at_ms`` is IMMUTABLE LOCAL RECEIPT TIME, stamped once on
    first durable insert and never rewritten: a retry or a reopen must not
    refresh it, or age becomes meaningless exactly when it matters.

    ``first_delivery_peer_id`` is the AUTHENTICATED connection or broker
    peer, or ``None``. It is never taken from the record's issuer field,
    any sender-controlled envelope field, an address guess, or the shared
    distribution key — all of those are attacker-chosen. Redelivery does
    not overwrite the first provenance. Nothing in this bead READS it; it
    is recorded now because attributing records already in flight is much
    harder than writing the column on day one.
    """

    record_type: str
    claimed_id: str
    unmet_dependency_kind: str
    unmet_dependency_id: str
    first_held_at_ms: int
    first_delivery_peer_id: str | None


@dataclass(frozen=True)
class PendingUsage:
    """Pending occupancy against its bounds. Both maxima are reported so a
    reader never has to guess which bound governs."""

    rows: int
    bytes: int
    max_rows: int
    max_bytes: int


@dataclass
class StoreCounters:
    """Injected instrumentation. Not statistics — these exist so tests can
    prove work was NOT done (rows not examined, wires not hydrated) without
    resorting to wall-clock timing, which passes on a fast machine with a
    quadratic algorithm."""

    retry_candidates: int = 0
    wire_hydrations: int = 0
    retry_terminal_refusals: int = 0


@dataclass(frozen=True)
class Locator:
    """Every stored bridge on one ``(child, parent)`` edge.

    PLURAL, deliberately. ``acceptance._has_one_valid_bridge`` returns
    ``valid == 1`` — EXACTLY one — so two signature-valid bodies for one
    edge make that state read history-incomplete. NO SOURCE SAYS WHAT
    SHOULD HAPPEN WITH TWO, so this store invents no policy: it retains
    both and MARKS THE EDGE AMBIGUOUS. Refusing the second, or silently
    preferring one, would be a protocol rule, and a protocol rule belongs
    in ``bb971a32-ed1`` beside the exactly-one predicate it would govern,
    not in an implementation. The implementable answer meanwhile is
    producer-side: do not mint an edge twice.

    ``bridge_ids`` is retained when a body is pruned as the AUDIT ANCHOR,
    not as the re-fetch key — §19 re-fetches by pair, because the id hashes
    the pruned body.
    """

    child_state_id: str
    parent_state_id: str
    bridge_ids: tuple
    body_present: frozenset
    ambiguous: bool


@dataclass(frozen=True)
class StoredState:
    """A KeyControlStore-level WRAPPER around :class:`acceptance.AcceptedState`,
    not an extension of it: persistence is a storage concern and the
    acceptance predicate has no store. ``bridges`` are the ``bridge_id``s
    this call persisted."""

    accepted: acceptance.AcceptedState
    bridges: tuple = ()


@dataclass
class _BridgeRow:
    """A stored bridge row. ``bridge`` is ``None`` when the body has been
    pruned (§19) — the row survives, locally unavailable."""

    child_state_id: str
    parent_state_id: str
    bridge: ParentBridge | None = None


class KeyControlStore:
    """A durable, self-verifying store of key-control records. One writer.

    Reuses ``org_ledger_db_path`` — pass a slug to target the org's own
    ``data/orgs/<slug>.db``, or an explicit path (``:memory:`` for a
    transient store). Every open hydrates the full descriptor set,
    content-address-verified, so the in-memory view can never hold a row
    the disk would reject.
    """

    def __init__(
        self,
        path=":memory:",
        *,
        slug=None,
        root=None,
        pending_limits: PendingLimits | None = None,
        counters: StoreCounters | None = None,
    ):
        if slug is not None:
            path = org_ledger_db_path(slug, root=root)
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, factory=FleetSyncConnection)
        if self.path != ":memory:":
            self.db.execute("PRAGMA journal_mode = WAL")
            # Two writers racing for the last pending slot must serialize,
            # not fail: the loser waits for the lock and then sees the
            # committed counters.
            self.db.execute("PRAGMA busy_timeout = 5000")
        self.pending_limits = pending_limits or PendingLimits()
        self.counters = counters or StoreCounters()
        with self.db:
            self.db.executescript(_SCHEMA)
            self.db.execute(
                "INSERT OR REPLACE INTO keycontrol_meta(key, value) VALUES"
                " ('schema_version', ?)",
                (str(KEYCONTROL_SCHEMA_VERSION),),
            )
            self.db.execute(
                "INSERT OR IGNORE INTO keycontrol_pending_usage"
                "(id, pending_rows, pending_bytes) VALUES (1, 0, 0)"
            )
        from tools.network.fleet_sync_sim.catalog import (
            attach_active_production_catalog,
        )
        self._fleet_catalog = attach_active_production_catalog(self.db)
        self._states: dict = {}  # state_id -> StorageStateDescriptor
        self._credentials: dict = {}  # kem_key_id -> PersonaKemCredential
        self._credentials_by_persona: dict = {}  # persona -> {kem_key_id: cred}
        self._bridge_rows: dict = {}  # bridge_id -> _BridgeRow
        self._bridge_edges: dict = {}  # (child, parent) -> set of bridge_id
        self._grants: dict = {}  # grant_id -> CapabilityGrant
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
        self._hydrate_credentials()
        self._hydrate_bridges()
        self._hydrate_grants()

    def _hydrate_bridges(self) -> None:
        rows = self.db.execute(
            "SELECT bridge_id, child_state_id, parent_state_id, wire "
            "FROM keycontrol_bridge"
        ).fetchall()
        for bridge_id, child_state_id, parent_state_id, wire in rows:
            bridge = None
            if wire is not None:
                # Full re-verification on every hydrate, not just at write:
                # content address AND signature, exactly as descriptors are.
                bridge = self._authenticate_bridge(bridge_id, bytes(wire))
                if (
                    bridge.child_state_id != child_state_id
                    or bridge.parent_state_id != parent_state_id
                ):
                    raise TamperError(
                        f"stored bridge {bridge_id[:12]} is indexed under an edge "
                        "its own bytes do not name"
                    )
            self._index_bridge(bridge_id, child_state_id, parent_state_id, bridge)

    def _index_bridge(self, bridge_id, child_state_id, parent_state_id, bridge) -> None:
        self._bridge_rows[bridge_id] = _BridgeRow(
            child_state_id=child_state_id,
            parent_state_id=parent_state_id,
            bridge=bridge,
        )
        self._bridge_edges.setdefault((child_state_id, parent_state_id), set()).add(
            bridge_id
        )

    def _hydrate_credentials(self) -> None:
        rows = self.db.execute(
            "SELECT kem_key_id, persona, wire FROM keycontrol_credential"
        ).fetchall()
        for kem_key_id, _persona, wire in rows:
            if wire is None:
                # Tail-body prune: the durable row remains the audit anchor,
                # but there is no credential to offer until a peer restores it.
                continue
            wire = bytes(wire)
            # Full re-verification on every hydrate, not just at write:
            # ``validate`` re-checks the Ed25519 signature and recomputes
            # ``compute_kem_key_id(binding_dict())`` — this family's content
            # address — so a tampered wire is caught at open.
            credential = credentials.validate(wire)
            if credential.kem_key_id != kem_key_id:
                raise TamperError(
                    f"stored credential {kem_key_id[:12]} does not match its "
                    "content address"
                )
            self._index_credential(credential)

    def _index_credential(self, credential: PersonaKemCredential) -> None:
        self._credentials[credential.kem_key_id] = credential
        self._credentials_by_persona.setdefault(credential.persona, {})[
            credential.kem_key_id
        ] = credential

    # -- write side ------------------------------------------------------------------

    def accept_state(
        self,
        descriptor: StorageStateDescriptor,
        fold_at,
        authority_ancestry,
        *,
        bridges=(),
        parent_descriptors=None,
    ) -> StoredState:
        """Verify *descriptor* against the authority fold, then persist it
        and the bridges offered with it.

        ``fold_at`` and ``authority_ancestry`` are the AUTHORITY-ledger
        seams :func:`acceptance.accept_state` folds at the record's cited
        frontier with (distinct from this store's storage-DAG
        :meth:`ancestry`). Acceptance runs FIRST: on any refusal its
        distinct :class:`~.acceptance.AcceptanceError` subclass propagates
        unchanged and no row is written.

        Returns a :class:`StoredState` WRAPPING the ``AcceptedState`` — read
        ``result.accepted.*`` for the acceptance outcome. The
        ``history_complete`` acceptance computed is over the CALLER-SUPPLIED
        bridge set and can disagree with disk; :meth:`history_complete` is
        the store-backed reading and is recomputed, never stored.

        A bridge offered here that fails its edge checks is SKIPPED, not
        raised on — §9 makes an invalid bridge yield history-incomplete
        rather than refuse the descriptor, and ``acceptance.accept_state``
        already treated it that way one line above. A bridge whose parent
        descriptor has not arrived is DEFERRED like any other.
        """
        result = acceptance.accept_state(
            descriptor,
            fold_at,
            authority_ancestry,
            bridges=bridges,
            parent_descriptors=parent_descriptors,
        )
        self._put(descriptor)  # also retries bridges pending on this state
        stored = []
        for offered in bridges:
            if not isinstance(offered, ParentBridge):
                continue
            wire = offered.to_json()
            claimed_id = record_id(wire)
            try:
                authenticated = self._authenticate_bridge(claimed_id, wire)
                outcome = self._admit_bridge(authenticated, wire)
            except StorageError:
                continue
            if outcome is Admission.ACCEPTED:
                stored.append(claimed_id)
        return StoredState(accepted=result, bridges=tuple(stored))

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
        # A newly held state may be exactly what a deferred bridge was
        # waiting for. Indexed by dependency — not a re-scan of everything
        # pending.
        self.retry_pending(DEPENDENCY_STATE, state_id)

    # -- bridges ---------------------------------------------------------------------

    def accept_bridge(
        self, claimed_id: str, wire: bytes, *, delivery_peer_id: str | None = None
    ) -> Admission:
        """Admit a :class:`ParentBridge` arriving on its own — ahead of its
        states, or as a §19 re-fetch.

        Takes RAW WIRE and a CLAIMED ID so canonical parse and content
        address are real tests of untrusted bytes. Three refusals are
        terminal and raise, writing nothing anywhere: a wire that is not in
        canonical byte form (:class:`~.errors.MalformedRecordError`), a
        ``claimed_id`` that is not SHA-256 of those bytes
        (:class:`TamperError`), and a signature that does not verify
        (:class:`~.errors.RecordSignatureError`). So are the edge checks
        against a HELD child descriptor (:class:`UnsignedEdgeError`,
        :class:`BridgeContextError`).

        When a named state is not yet held the bridge is DEFERRED: the store
        retains the wire and retries when that state arrives, with no
        resubmission by the sender. At the pending bound an unrelated record
        gets a NON-DURABLE ``RETRY_LATER_CAPACITY`` — nothing is evicted and
        it is admitted once capacity frees.

        *delivery_peer_id* is the AUTHENTICATED connection or broker peer,
        supplied by the transport that authenticated it. Pass ``None``
        rather than inventing one: no field of *wire* may reach it, and a
        record trying to carry its own provenance is refused by canonical
        parse before this argument is ever read.
        """
        bridge = self._authenticate_bridge(claimed_id, wire)
        return self._admit_bridge(bridge, bytes(wire), delivery_peer_id=delivery_peer_id)

    def _authenticate_bridge(self, claimed_id, wire) -> ParentBridge:
        """Canonical parse, content address, signature — in that order."""
        if not isinstance(wire, (bytes, bytearray)):
            raise MalformedRecordError("bridge wire must be bytes")
        wire = bytes(wire)
        bridge = ParentBridge.from_json(wire)  # anti-malleable strict parse
        if record_id(wire) != claimed_id:
            raise TamperError(
                f"bridge {str(claimed_id)[:12]} does not match its content address"
            )
        bridge_mod.verify_signature(bridge)
        return bridge

    def _admit_bridge(
        self, bridge: ParentBridge, wire: bytes, *, delivery_peer_id=None
    ) -> Admission:
        """Dependency, edge validity, persistence. *bridge* is already
        authenticated against *wire*."""
        bridge_id = record_id(wire)
        child = self._states.get(bridge.child_state_id)
        if child is None:
            return self._defer(
                RECORD_TYPE_BRIDGE,
                bridge_id,
                DEPENDENCY_STATE,
                bridge.child_state_id,
                wire,
                delivery_peer_id,
            )
        self._require_signed_edge(bridge, child)
        if bridge.parent_state_id not in self._states:
            # The parent descriptor carries the commitment every recovered
            # secret is checked against; without it a stored bridge could
            # not be followed anyway.
            return self._defer(
                RECORD_TYPE_BRIDGE,
                bridge_id,
                DEPENDENCY_STATE,
                bridge.parent_state_id,
                wire,
                delivery_peer_id,
            )
        self._put_bridge(bridge_id, bridge, wire)
        self.drop_pending(RECORD_TYPE_BRIDGE, bridge_id)
        return Admission.ACCEPTED

    def _require_signed_edge(self, bridge: ParentBridge, child) -> None:
        """The bridge belongs to the one advance operation its descriptor's
        acceptance already authorized (§8 / §9). NO independent fold check
        and NO roster resolution of ``issuer_persona`` happen here — the
        scope check happened once, on the descriptor."""
        if bridge.parent_state_id not in child.parent_state_ids:
            raise UnsignedEdgeError(
                f"bridge names parent {bridge.parent_state_id[:12]}, which is not "
                f"in child state {bridge.child_state_id[:12]}'s signed parents"
            )
        if bridge.issuer_persona != child.creator_persona:
            raise BridgeContextError(
                "bridge issuer_persona does not equal its descriptor's creator_persona"
            )
        if tuple(bridge.authority_heads) != tuple(child.authority_heads):
            raise BridgeContextError(
                "bridge authority_heads do not equal its descriptor's authority_heads"
            )
        if bridge.genesis_id != child.genesis_id:
            raise BridgeContextError("bridge is bound to another organization")
        if bridge.domain_id != child.domain_id:
            raise BridgeContextError("bridge names another storage domain")

    def _put_bridge(self, bridge_id: str, bridge: ParentBridge, wire: bytes) -> None:
        row = self._bridge_rows.get(bridge_id)
        if row is not None and row.bridge is not None:
            # Content-addressed dedupe (§1e): same key ⇒ same wire (SHA-256),
            # so re-storing is idempotent and writes no second row.
            return
        with self.db:
            if row is None:
                self.db.execute(
                    "INSERT OR IGNORE INTO keycontrol_bridge"
                    "(bridge_id, child_state_id, parent_state_id, wire)"
                    " VALUES (?, ?, ?, ?)",
                    (bridge_id, bridge.child_state_id, bridge.parent_state_id, wire),
                )
            else:
                # A §19 re-fetch refilling a pruned body. The row — and its
                # audit anchor — never went away.
                self.db.execute(
                    "UPDATE keycontrol_bridge SET wire = ? WHERE bridge_id = ?",
                    (wire, bridge_id),
                )
        self._index_bridge(
            bridge_id, bridge.child_state_id, bridge.parent_state_id, bridge
        )

    def prune_bridge_body(self, bridge_id: str) -> bool:
        """Drop a bridge BODY at the tail (crib §19), keeping the row.

        The identifier survives as the audit anchor; the edge stays
        resolvable through :meth:`bridge_locator`, which is how the body is
        re-fetched — by pair, never by ``record_id``, because the id hashes
        the bytes that were pruned. Returns True when a body was removed.
        """
        row = self._bridge_rows.get(bridge_id)
        if row is None or row.bridge is None:
            return False
        with self.db:
            self.db.execute(
                "UPDATE keycontrol_bridge SET wire = NULL WHERE bridge_id = ?",
                (bridge_id,),
            )
        row.bridge = None
        return True

    def accept_credential(self, credential) -> PersonaKemCredential:
        """Fully authenticate a :class:`PersonaKemCredential`, then persist it.

        Verification precedes retention: :func:`credentials.validate`
        checks the Ed25519 signature over ``signing_input()``, exact
        ``_FIELDS`` closure, version, suite, hex widths, and that
        ``compute_kem_key_id(binding_dict())`` equals the stored
        ``kem_key_id``. Any failure raises its distinct
        :class:`~.errors.StorageError` subclass, which propagates unchanged
        — the credential is refused TERMINALLY and no row is written.

        Admission deliberately does NOT call ``verify_against_fold``: that
        requires a fold this store does not hold and answers a TIME-VARYING
        question (a rekey-retired key is fold-invalid though not one byte
        changed). Currency is the reader's question, resolved by
        :func:`select_current_credential`. So a credential whose persona is
        absent from any fold still stores and retrieves.

        Accepts the wire bytes, the payload dict, or a record (whatever
        :func:`credentials.validate` accepts) and returns the validated
        record.
        """
        validated = credentials.validate(credential)
        self._put_credential(validated)
        return validated

    def _put_credential(self, credential: PersonaKemCredential) -> None:
        kem_key_id = credential.kem_key_id
        if kem_key_id in self._credentials:
            # Content-addressed dedupe (§1e): ``kem_key_id`` is the SHA-256
            # of the binding and the persona signs deterministically, so an
            # identical credential is identical bytes. Re-storing is a no-op.
            return
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO keycontrol_credential"
                "(kem_key_id, persona, wire) VALUES (?, ?, ?)",
                (kem_key_id, credential.persona, credential.to_json()),
            )
        self._index_credential(credential)

    # -- pending records ---------------------------------------------------------------

    def _defer(
        self,
        record_type: str,
        claimed_id: str,
        dependency_kind: str,
        dependency_id: str,
        wire: bytes,
        delivery_peer_id,
    ) -> Admission:
        """Hold *wire* durably until *dependency_id* arrives.

        UNIQUENESS IS CHECKED BEFORE CAPACITY, so a duplicate redelivery
        arriving at a full boundary stays DEFERRED and consumes zero new
        capacity rather than being spuriously refused. A redelivery updates
        only which dependency is currently unmet — never
        ``first_held_at_ms``, never ``first_delivery_peer_id``.
        """
        if delivery_peer_id is not None and not isinstance(delivery_peer_id, str):
            raise MalformedRecordError("delivery_peer_id must be a string or None")
        held = self.db.execute(
            "SELECT 1 FROM keycontrol_pending WHERE record_type = ? AND claimed_id = ?",
            (record_type, claimed_id),
        ).fetchone()
        if held is not None:
            with self.db:
                self.db.execute(
                    "UPDATE keycontrol_pending SET unmet_dependency_kind = ?,"
                    " unmet_dependency_id = ? WHERE record_type = ? AND claimed_id = ?",
                    (dependency_kind, dependency_id, record_type, claimed_id),
                )
            return Admission.DEFERRED
        size = len(wire)
        limits = self.pending_limits
        try:
            with self.db:
                cursor = self.db.execute(
                    _INSERT_PENDING_GUARDED,
                    (
                        record_type,
                        claimed_id,
                        dependency_kind,
                        dependency_id,
                        _now_ms(),
                        delivery_peer_id,
                        wire,
                        size,
                        limits.max_rows,
                        size,
                        limits.max_bytes,
                    ),
                )
                if cursor.rowcount != 1:
                    # At the bound: evict NOTHING, refuse NON-DURABLY, leave
                    # the containing batch unacknowledged upstream so it is
                    # retried. Whoever reaches the last slot takes it — this
                    # is first-arrival behaviour at the resource edge, and
                    # what makes it acceptable is LOSSLESSNESS, not
                    # order-independence: nothing is discarded and the record
                    # converges once capacity frees. Eviction would be
                    # strictly worse — it can discard the honest record while
                    # retaining a poisoned one.
                    return Admission.RETRY_LATER_CAPACITY
                self.db.execute(
                    "UPDATE keycontrol_pending_usage SET pending_rows = pending_rows + 1,"
                    " pending_bytes = pending_bytes + ? WHERE id = 1",
                    (size,),
                )
        except sqlite3.IntegrityError:
            # Lost a race to another writer inserting the same record. It is
            # held; that is what DEFERRED means.
            return Admission.DEFERRED
        return Admission.DEFERRED

    def retry_pending(self, dependency_kind: str, dependency_id: str) -> int:
        """Re-evaluate ONLY the pending records awaiting *dependency_id*.

        Indexed on ``(unmet_dependency_kind, unmet_dependency_id)``, not a
        full re-scan: when an identifier arrives, unrelated pending wires
        are neither hydrated nor re-verified. The harness's drive loop
        retries every pending item on every pass, which is correct for a
        handful and quadratic for a backlog — a sender who merely wants to
        make legitimate arrivals slow does not need to fill the disk, only
        to grow the queue. This is a correctness property of the retry path,
        not a policy about who may fill it.

        One pass, no loop: nothing in this store is a dependency OF a
        bridge, so an admitted bridge cannot unblock a further row. Returns
        the number of records admitted.
        """
        rows = self.db.execute(
            "SELECT record_type, claimed_id, wire FROM keycontrol_pending"
            " WHERE unmet_dependency_kind = ? AND unmet_dependency_id = ?",
            (dependency_kind, dependency_id),
        ).fetchall()
        admitted = 0
        for record_type, claimed_id, wire in rows:
            self.counters.retry_candidates += 1
            if record_type != RECORD_TYPE_BRIDGE:
                continue  # no other family persists here yet
            self.counters.wire_hydrations += 1
            try:
                bridge = self._authenticate_bridge(claimed_id, bytes(wire))
                outcome = self._admit_bridge(bridge, bytes(wire))
            except StorageError:
                # Terminal: the record can never be admitted — a forged edge,
                # a tampered row. Dropping it is not eviction under pressure;
                # it is the refusal that was deferred until the descriptor
                # needed to judge it arrived.
                self.counters.retry_terminal_refusals += 1
                self.drop_pending(record_type, claimed_id)
                continue
            if outcome is Admission.ACCEPTED:
                admitted += 1
        return admitted

    def drop_pending(self, record_type: str, claimed_id: str) -> bool:
        """Remove one pending record. NOTHING IS EVICTED AUTOMATICALLY —
        this is the explicit action, called by an operator or by the store
        itself only when the record has been admitted or terminally
        refused."""
        row = self.db.execute(
            "SELECT wire_len FROM keycontrol_pending"
            " WHERE record_type = ? AND claimed_id = ?",
            (record_type, claimed_id),
        ).fetchone()
        if row is None:
            return False
        with self.db:
            self.db.execute(
                "DELETE FROM keycontrol_pending"
                " WHERE record_type = ? AND claimed_id = ?",
                (record_type, claimed_id),
            )
            self.db.execute(
                "UPDATE keycontrol_pending_usage SET pending_rows = pending_rows - 1,"
                " pending_bytes = pending_bytes - ? WHERE id = 1",
                (row[0],),
            )
        return True

    @staticmethod
    def _pending_filter(record_type, dependency_kind, dependency_id) -> tuple:
        clauses, params = [], []
        if record_type is not None:
            clauses.append("record_type = ?")
            params.append(record_type)
        if dependency_kind is not None:
            clauses.append("unmet_dependency_kind = ?")
            params.append(dependency_kind)
        if dependency_id is not None:
            clauses.append("unmet_dependency_id = ?")
            params.append(dependency_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, tuple(params)

    def pending_count(
        self, *, record_type=None, dependency_kind=None, dependency_id=None
    ) -> int:
        """How many records are held pending, filterable by FAMILY and by
        DEPENDENCY. Aggregate-queryable: both filters are index prefixes, so
        this never walks every row."""
        where, params = self._pending_filter(
            record_type, dependency_kind, dependency_id
        )
        return self.db.execute(
            f"SELECT COUNT(*) FROM keycontrol_pending{where}", params
        ).fetchone()[0]

    def pending_records(
        self, *, record_type=None, dependency_kind=None, dependency_id=None, limit=None
    ) -> tuple:
        """Enumerate pending records WITHOUT hydrating their wire bodies.

        The ``wire`` column is not selected and no record is parsed — an
        operator listing a backlog must not pay to deserialize it, and must
        not be able to be made to.
        """
        where, params = self._pending_filter(
            record_type, dependency_kind, dependency_id
        )
        sql = (
            "SELECT record_type, claimed_id, unmet_dependency_kind,"
            " unmet_dependency_id, first_held_at_ms, first_delivery_peer_id"
            f" FROM keycontrol_pending{where} ORDER BY first_held_at_ms, claimed_id"
        )
        if limit is not None:
            sql += " LIMIT ?"
            params = (*params, int(limit))
        return tuple(
            PendingRecordInfo(*row) for row in self.db.execute(sql, params).fetchall()
        )

    def pending_usage(self) -> PendingUsage:
        """Occupancy against both bounds. The counters are maintained in the
        same transaction as every insert and delete, so they survive a reopen
        and never need a table walk to read."""
        rows, nbytes = self.db.execute(
            "SELECT pending_rows, pending_bytes FROM keycontrol_pending_usage"
            " WHERE id = 1"
        ).fetchone()
        return PendingUsage(
            rows=rows,
            bytes=nbytes,
            max_rows=self.pending_limits.max_rows,
            max_bytes=self.pending_limits.max_bytes,
        )

    # -- read side -------------------------------------------------------------------

    def get(self, state_id: str):
        """The stored :class:`StorageStateDescriptor`, or ``None``."""
        return self._states.get(state_id)

    def get_credential(self, kem_key_id: str):
        """The one stored :class:`PersonaKemCredential` at *kem_key_id*, or
        ``None``. Grants address ``recipient_kem_key_id``; this is that lookup."""
        return self._credentials.get(kem_key_id)

    def credentials_for_persona(self, persona: str) -> list:
        """Every credential *persona* has published, ascending by
        ``kem_key_id``.

        The full candidate set — not the current one. Currency is not
        uniqueness in storage: :func:`select_current_credential` needs every
        candidate, and discarding the older would destroy its input. The
        caller runs that selection with its own LEDGER ancestry, never this
        store's :meth:`ancestry`.
        """
        by_id = self._credentials_by_persona.get(persona, {})
        return [by_id[k] for k in sorted(by_id)]

    def get_bridge(self, bridge_id: str):
        """The stored :class:`ParentBridge`, or ``None`` — including when the
        row is present but its body has been pruned (§19), which is
        LOCALLY UNAVAILABLE, not absent. :meth:`bridge_locator` distinguishes
        the two."""
        row = self._bridge_rows.get(bridge_id)
        return None if row is None else row.bridge

    def bridge_locator(self, child_state_id: str, parent_state_id: str):
        """Every stored bridge on one edge, or ``None`` if the edge is unknown.

        Resolves through the ``(child, parent)`` index — no table scan — and
        STILL RESOLVES WHEN THE BODY IS LOCALLY ABSENT, which is the point:
        §19 re-fetches by pair, not by ``record_id``, because the id hashes
        the pruned body.
        """
        ids = self._bridge_edges.get((child_state_id, parent_state_id))
        if not ids:
            return None
        ordered = tuple(sorted(ids))
        return Locator(
            child_state_id=child_state_id,
            parent_state_id=parent_state_id,
            bridge_ids=ordered,
            body_present=frozenset(
                b for b in ordered if self._bridge_rows[b].bridge is not None
            ),
            ambiguous=len(ordered) > 1,
        )

    def _edge_bodies(self, child_state_id: str, parent_state_id: str) -> list:
        """The locally-available bridges on one edge. A NULL body is never
        hydrated as a ``ParentBridge`` — it is simply not here."""
        return [
            self._bridge_rows[b].bridge
            for b in sorted(self._bridge_edges.get((child_state_id, parent_state_id), ()))
            if self._bridge_rows[b].bridge is not None
        ]

    def accepted_bridges(self) -> tuple:
        """Every stored bridge whose BODY is locally present, ascending by
        ``bridge_id``. Pruned rows are excluded — they are audit anchors, not
        bridges."""
        return tuple(
            row.bridge
            for _, row in sorted(self._bridge_rows.items())
            if row.bridge is not None
        )

    def accept_grant(self, grant) -> str:
        """Persist one capability grant, content-addressed by ``grant_id``.

        Idempotent — the same grant re-offered writes no second row. The
        grant's signature and fold context are verified on USE
        (``open_generation_keys`` opens it against the descriptor), not here:
        this store's one job is to keep the record durable. A grant whose
        generation secret was held only in the process's in-memory cache is
        exactly the loss this closes — persisted, it lets an unlock re-derive
        the recipient's KEM key from the root and recover the generation key.
        """
        wire = grant.to_json()
        grant_id = grant.grant_id
        if record_id(wire) != grant_id:  # invariant guard; true for a real record
            raise TamperError("grant_id does not equal record_id(wire)")
        if grant_id in self._grants:
            return grant_id
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO keycontrol_grant"
                "(grant_id, storage_state_id, recipient_kem_key_id, wire)"
                " VALUES (?, ?, ?, ?)",
                (grant_id, grant.storage_state_id, grant.recipient_kem_key_id, wire),
            )
        self._grants[grant_id] = grant
        return grant_id

    def accepted_grants(self) -> tuple:
        """Every stored capability grant, ascending by ``grant_id``.

        The durable recovery material an unlock reads: for each grant, the
        recipient re-derives their KEM key from the root and opens it to rebuild
        a generation key the in-memory cache lost on restart."""
        return tuple(g for _, g in sorted(self._grants.items()))

    def _hydrate_grants(self) -> None:
        from .capability import CapabilityGrant

        rows = self.db.execute(
            "SELECT grant_id, wire FROM keycontrol_grant"
        ).fetchall()
        for grant_id, wire in rows:
            # Anti-malleable strict parse; the grant's own grant_id equals
            # record_id(wire) by construction, re-checked here.
            grant = CapabilityGrant.from_json(bytes(wire))
            if grant.grant_id != grant_id:
                raise TamperError(
                    f"stored grant {grant_id[:12]} is indexed under an id its "
                    "own bytes do not name"
                )
            self._grants[grant_id] = grant

    def history_complete(self, state_id: str) -> bool:
        """Whether every signed parent edge of *state_id* has exactly one
        locally-available valid bridge — RECOMPUTED from the store, never
        stored.

        This tracks LOCAL AVAILABILITY, not admission: a state admitted with
        a bridge still in flight reads False without raising (§9's
        admission-time false branch is deliberate), goes True when the bridge
        arrives, goes False again when the body is pruned (§19), and True
        again when it is re-fetched. A column holding this would be wrong the
        moment any of those happened.

        Delegates the predicate itself to ``acceptance`` so "exactly one
        valid bridge" has ONE definition; the edge index means it is asked
        only about the edge's own bodies.
        """
        descriptor = self._states.get(state_id)
        if descriptor is None:
            raise UnknownStateError(
                f"history_complete asked about unseen state {state_id[:12]!r}"
            )
        return all(
            acceptance._has_one_valid_bridge(
                descriptor, parent_id, self._edge_bodies(state_id, parent_id)
            )
            for parent_id in descriptor.parent_state_ids
        )

    def recover_ancestors(self, held_state_id: str, held_state_secret: bytes) -> dict:
        """Every reachable ancestor secret from a held state, through the
        STORED bridges. :func:`bridge.recover_ancestors`, unchanged.

        Child reaches parent; parent never reaches child. A holder of a
        parent secret recovers nothing newer — the edge key is HKDF-Expand
        over the CHILD secret, and nothing about a child is derivable from a
        parent.

        This is also where a poisoned body is discovered: the store cannot
        derive ``edge_key`` and so cannot authenticate a bridge's AEAD at
        admission. A body that does not open raises
        :class:`~.bridge.BridgeError` here; one that opens to a secret
        failing its parent's commitment raises
        :class:`~.errors.CommitmentError`.
        """
        return bridge_mod.recover_ancestors(
            held_state_id,
            held_state_secret,
            self.accepted_bridges(),
            self._states,
        )

    def __contains__(self, state_id: str) -> bool:
        return state_id in self._states

    def __len__(self) -> int:
        return len(self._states)

    @property
    def states(self) -> dict:
        """A snapshot copy of ``state_id -> descriptor``."""
        return dict(self._states)

    @tag_dag(STORAGE)
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
