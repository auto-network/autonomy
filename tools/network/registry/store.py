"""SQLite persistence for the registry.

Plain ``sqlite3``, one connection, WAL mode. The store holds *state*, not
*authority*: nothing in these tables grants permission — authorization is
re-derived per request from the delegation chain in the envelope (I4).

Tables:

- ``orgs`` — bindings: org UUID → root pub, recovery policy, TTL state.
- ``rebinds`` — key-continuity audit trail (old root → new root), so
  trust history can attach to *UUID + key-continuity chain* (spec §4.3).
- ``links`` — share-link grants; every row records the certificate
  subject that minted it (I6: promptless ≠ traceless).
- ``revocations`` — verified revocation records, retained only until the
  revoked key's natural expiry (I7); ``purge_expired`` is the sweep.
- ``topic_hints`` / ``topic_bundles`` — the F3 ledger-sync broker path
  (spec §6, L6): per-org topics carrying 32-byte head hints and a
  store-and-forward mailbox of ENCRYPTED event bundles. Both tables are
  T0-blind by construction — they hold topic names, event hashes, sizes,
  and opaque ciphertext; plaintext event bytes never reach this store.
- ``listings`` / ``attestations`` — the L1 listing directory
  (``graph://29ff28a8-b39``): signed listing CARDS keyed
  ``(publisher, name)`` — never bundle bytes — and third-party attestation
  records keyed by subject public key. Listing rows record the publisher's
  bound root at accept time (``root_pub``), which is what the
  key-continuity update rule anchors to. Unlike links, listings survive an
  expiry-reclaim of the org UUID: the chain belongs to the key-continuity,
  not the binding, so a reclaimer can neither extend nor revoke it.
- ``witness_log`` — the F4 equivocation witness (spec §6 role 2, L5): a
  per-``(org, topic)`` APPEND-ONLY, hash-chained log of the head-sets
  members publish. Each row commits to the previous (``prev_id``), so the
  served chain is tamper-evident; the store exposes only append + read,
  never update or delete, which is what "append-only" means at this layer.
  T1-blind like the broker tables — hashes, a publisher pubkey, and a
  timestamp only; it composes with L6 because it never needs a plaintext.

Timestamps are unix seconds unless explicitly named ``*_ms``. They are passed
in by the caller — the store never reads the wall clock, which is what makes
TTL behavior testable.
"""

from __future__ import annotations

import functools
import json
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from typing import Callable, Optional

from tools.network.idkit import RevocationRecord, RevocationSet

from .witness import (
    WITNESS_TOPICS,
    build_entry,
    build_entry_v2,
    entry_id as _entry_id,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS orgs (
    org_uuid        TEXT PRIMARY KEY,
    root_pub        TEXT NOT NULL,
    recovery_policy TEXT NOT NULL,
    recovery_pub    TEXT,
    created_at      INTEGER NOT NULL,
    expires_at      INTEGER NOT NULL,
    renewed_at      INTEGER,
    endpoint_hints  TEXT,
    -- Monotonic recovery-policy version. Bumped by every root-signed policy
    -- update AND by rebind (one shared counter), compared-and-swapped so an
    -- old policy envelope cannot be replayed (registry policy-update, F1/F2).
    policy_epoch    INTEGER NOT NULL DEFAULT 0,
    binding_generation TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rebinds (
    org_uuid     TEXT NOT NULL,
    old_root_pub TEXT NOT NULL,
    new_root_pub TEXT NOT NULL,
    rebound_at   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS links (
    token        TEXT PRIMARY KEY,
    org_uuid     TEXT NOT NULL,
    target_uuid  TEXT NOT NULL,
    target_type  TEXT NOT NULL,
    invite_ref   TEXT,
    meta         TEXT NOT NULL,
    created_at   INTEGER NOT NULL,
    expires_at   INTEGER,
    -- Client-supplied absolute unix-ms expiry for org:join grants. Kept
    -- separate from the legacy relative-TTL unix-seconds column so old rows
    -- and non-join grants retain their exact semantics.
    expires_at_ms INTEGER,
    revoked_at   INTEGER,
    -- signer_pub / subject_id are NULL for org-tunnel grants (register
    -- D19): a link minted over the authenticated tunnel is an act of the
    -- tunnel's org, and the registry never sees a persona on that path.
    signer_pub   TEXT,
    subject_kind TEXT NOT NULL,
    subject_id   TEXT,
    operation_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_links_org ON links (org_uuid);

CREATE TABLE IF NOT EXISTS link_operations (
    org_uuid                  TEXT NOT NULL,
    operation_id              TEXT NOT NULL,
    operation                 TEXT NOT NULL,
    binding_root_pub          TEXT NOT NULL,
    binding_generation        TEXT NOT NULL,
    receipt_request_digest    TEXT NOT NULL,
    acceptance_envelope_digest TEXT NOT NULL,
    registry_input_digest     TEXT NOT NULL,
    local_intent_digest       TEXT NOT NULL,
    operand_digest            TEXT,
    origin_proof_commitment   TEXT NOT NULL,
    source_expires_at_ms      INTEGER,
    accepted_at               INTEGER NOT NULL,
    signer_pub                TEXT NOT NULL,
    subject_kind              TEXT NOT NULL,
    subject_id                TEXT NOT NULL,
    receipt_json              TEXT NOT NULL,
    receipt_signature         TEXT NOT NULL,
    state                     TEXT NOT NULL,
    result_token              TEXT,
    completed_at              INTEGER,
    PRIMARY KEY (org_uuid, operation_id)
);

CREATE TABLE IF NOT EXISTS revocations (
    org_uuid       TEXT NOT NULL,
    revoked_key_id TEXT NOT NULL,
    record         TEXT NOT NULL,
    expires_at     INTEGER NOT NULL,
    PRIMARY KEY (org_uuid, revoked_key_id)
);
CREATE INDEX IF NOT EXISTS idx_revocations_expiry ON revocations (expires_at);

CREATE TABLE IF NOT EXISTS topic_hints (
    org_uuid     TEXT NOT NULL,
    topic        TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    heads        TEXT NOT NULL,
    signer_pub   TEXT NOT NULL,
    published_at INTEGER NOT NULL,
    PRIMARY KEY (org_uuid, topic, seq)
);

CREATE TABLE IF NOT EXISTS topic_bundles (
    org_uuid     TEXT NOT NULL,
    topic        TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    v            INTEGER NOT NULL,
    hashes       TEXT NOT NULL,
    size         INTEGER NOT NULL,
    ciphertext   BLOB NOT NULL,
    signer_pub   TEXT NOT NULL,
    deposited_at INTEGER NOT NULL,
    PRIMARY KEY (org_uuid, topic, seq)
);

CREATE TABLE IF NOT EXISTS listings (
    publisher    TEXT NOT NULL,
    name         TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    listing_id   TEXT NOT NULL,
    prev_id      TEXT,
    version      TEXT NOT NULL,
    bundle_hash  TEXT NOT NULL,
    claim        TEXT NOT NULL,
    root_pub     TEXT NOT NULL,
    signer_pub   TEXT NOT NULL,
    subject_kind TEXT NOT NULL,
    subject_id   TEXT NOT NULL,
    created_at   INTEGER NOT NULL,
    revoked_at   INTEGER,
    PRIMARY KEY (publisher, name, seq)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_listings_id ON listings (listing_id);
CREATE INDEX IF NOT EXISTS idx_listings_name ON listings (name);

-- One row per LOGICAL claim (who says what about whom): a refresh with a
-- newer ts replaces its predecessor, so honest re-attestation never grows
-- the table and a single attestor can hold at most one live row per
-- (subject, claim_type, claim_value) — the same keep-the-newer discipline
-- revocations use.
CREATE TABLE IF NOT EXISTS attestations (
    attestor_pub   TEXT NOT NULL,
    subject_pub    TEXT NOT NULL,
    claim_type     TEXT NOT NULL,
    claim_value    TEXT NOT NULL,
    attestation_id TEXT NOT NULL,
    record         TEXT NOT NULL,
    ts             INTEGER NOT NULL,
    expires_at     INTEGER NOT NULL,
    received_at    INTEGER NOT NULL,
    PRIMARY KEY (attestor_pub, subject_pub, claim_type, claim_value)
);
CREATE INDEX IF NOT EXISTS idx_attestations_subject ON attestations (subject_pub);
CREATE INDEX IF NOT EXISTS idx_attestations_expiry ON attestations (expires_at);

CREATE TABLE IF NOT EXISTS witness_log (
    org_uuid     TEXT NOT NULL,
    topic        TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    heads        TEXT NOT NULL,
    prev_id      TEXT,
    entry_id     TEXT NOT NULL,
    publisher    TEXT NOT NULL,
    witnessed_at INTEGER NOT NULL,
    PRIMARY KEY (org_uuid, topic, seq)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_witness_entry
    ON witness_log (org_uuid, topic, entry_id);

-- Witness v2 (auto-jqd9q): one grouped, timestamped chain per org. The v1
-- table above is retained read-only for archived chains.
CREATE TABLE IF NOT EXISTS witness_log_v2 (
    org_uuid     TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    prev_id      TEXT,
    t            INTEGER NOT NULL,
    heads_json   TEXT NOT NULL,
    entry_id     TEXT NOT NULL,
    publisher    TEXT NOT NULL,
    witnessed_at INTEGER NOT NULL,
    PRIMARY KEY (org_uuid, seq)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_witness_v2_entry
    ON witness_log_v2 (org_uuid, entry_id);

-- G1 node reachability hints (spec §8) --------------------------------------

-- Self-announced connectivity candidates for an org's nodes: direct-dial
-- address candidates (the ICE-style seed for hole-punching) and, when the
-- node offers to relay for its org, the dial URL of its peer relay. The
-- node's identity IS the announcing envelope's signer key; a node can only
-- ever announce itself. Rows expire by TTL (stale hints must die — a
-- reconnecting laptop's old address is worse than none) and are re-upserted
-- by each refresh. Tier B by construction: reads require a signed envelope,
-- so anonymous sessions can never enumerate an org's interior addresses.
CREATE TABLE IF NOT EXISTS node_hints (
    org_uuid     TEXT NOT NULL,
    node_pub     TEXT NOT NULL,
    addrs        TEXT NOT NULL,
    relay_url    TEXT,
    announced_at INTEGER NOT NULL,
    expires_at   INTEGER NOT NULL,
    PRIMARY KEY (org_uuid, node_pub)
);
CREATE INDEX IF NOT EXISTS idx_node_hints_expiry ON node_hints (expires_at);

-- auto-0zdky serving hostname ownership --------------------------------------

-- Durable persona-bound hostname ownership: one row per
-- NamespaceReservation (UUIDv5 over persona_pub + app label). The row is
-- the *advertisement* accepted from an authenticated host-register — it
-- carries no session, container, port, grant, or route. The live lease
-- binding a reservation to one connection is deliberately memory-only
-- (it must die with the connection); only the monotonic generation
-- counter persists here so a registry restart can never resurrect a
-- pre-restart generation and un-fence a stale renewal.
CREATE TABLE IF NOT EXISTS serve_hosts (
    reservation_id TEXT PRIMARY KEY,
    org_uuid       TEXT NOT NULL,
    persona_pub    TEXT NOT NULL,
    host           TEXT NOT NULL UNIQUE,
    generation     INTEGER NOT NULL,
    created_at     INTEGER NOT NULL
);

-- DNS-01 challenge TXT values served by the registry's own authoritative
-- responder (auto-g1jxw: the DNS server IS the registry). Every value
-- carries an expiry; reads purge inline, so a crashed ACME client can
-- never strand a challenge. Writes arrive only through the bounded
-- dns_challenges module (later: the auto-bhs3c authenticated control op).
CREATE TABLE IF NOT EXISTS serve_challenges (
    name       TEXT NOT NULL,
    value      TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    ttl        INTEGER NOT NULL DEFAULT 60,
    order_ref  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (name, value)
);

-- One immutable serving label per persona (design §3.2: generated once,
-- never renamed). UNIQUE(label) is the registry-side rejection of the
-- astronomically-unlikely same-label/different-key collision — refused,
-- never silently renamed.
CREATE TABLE IF NOT EXISTS serve_labels (
    persona_pub TEXT PRIMARY KEY,
    label       TEXT NOT NULL UNIQUE,
    created_at  INTEGER NOT NULL
);

-- auto-e2ufw: the per-org allow-set of registered SERVING machine public
-- keys. Onboarding/backfill registers the unlinkable serving-machine pubkey
-- for each (org, machine); the tunnel hello verify hard-enforces membership
-- once an org's set is non-empty (Option B, graph://a374b260-e4a). A pubkey
-- is public and org-scoped: org A's row never reveals org B's serving key.
CREATE TABLE IF NOT EXISTS serve_machine_keys (
    org_uuid    TEXT NOT NULL,
    machine_pub TEXT NOT NULL,
    created_at  INTEGER NOT NULL,
    PRIMARY KEY (org_uuid, machine_pub)
);

-- E1 session linking (spec §4.7, §4.8, §6.8) --------------------------------

-- First-party auto.network viewing sessions. A row is created ANONYMOUS
-- (identified=0, no org/subject) when a browser mints a QR challenge, and
-- UPGRADED in place to identified=1 when an assertion redeems. Identity
-- attaches ONLY through redemption (I12) — never from a bearer-link view.
CREATE TABLE IF NOT EXISTS link_sessions (
    session_id       TEXT PRIMARY KEY,
    org_uuid         TEXT,
    subject_kind     TEXT,
    subject_id       TEXT,
    dashboard_origin TEXT,
    identified       INTEGER NOT NULL,
    created_at       INTEGER NOT NULL,
    expires_at       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_link_sessions_expiry ON link_sessions (expires_at);

-- QR cross-device challenges (§4.8): an anonymous browser mints a ~60s
-- nonce bound to ITS session; the trusted PWA later redeems it (via §4.7,
-- nonce inside the signed assertion) to upgrade that bound session — not
-- the submitter's. Single-use: redeemed_at is set exactly once.
CREATE TABLE IF NOT EXISTS link_challenges (
    nonce       TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    created_at  INTEGER NOT NULL,
    expires_at  INTEGER NOT NULL,
    redeemed_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_link_challenges_expiry ON link_challenges (expires_at);

-- Anti-replay for redeemed assertion nonces (§4.7). Retention is bounded
-- by the assertion's own not_after (I7 discipline): once an assertion can
-- no longer be fresh, remembering its nonce buys nothing.
CREATE TABLE IF NOT EXISTS consumed_assertions (
    nonce      TEXT PRIMARY KEY,
    org_uuid   TEXT NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_consumed_assertions_expiry ON consumed_assertions (expires_at);

-- I12 witness: identity is recorded against a VIEW only when the grant
-- required auth. This table exists so the invariant is assertable on
-- storage — for a plain bearer (no-auth) grant it stays empty even when
-- the viewing session is identified.
CREATE TABLE IF NOT EXISTS link_view_attributions (
    session_id   TEXT NOT NULL,
    token        TEXT NOT NULL,
    subject_kind TEXT NOT NULL,
    subject_id   TEXT NOT NULL,
    org_uuid     TEXT NOT NULL,
    viewed_at    INTEGER NOT NULL,
    PRIMARY KEY (session_id, token)
);

-- Committed membership (graph://da0dd9fb-e75, auto-1wxet): the registry's
-- ONE verified tuple per org — adopted by checkpoint induction from a
-- root-signed seed, never by folding a ledger. `checkpoint` stores the full
-- signed record so the induction can continue and replay can compare.
CREATE TABLE IF NOT EXISTS membership_state (
    org_uuid           TEXT PRIMARY KEY,
    seq                INTEGER NOT NULL,
    members_root       TEXT NOT NULL,
    checkpointers_root TEXT NOT NULL,
    ledger_head        TEXT NOT NULL,
    checkpoint         TEXT NOT NULL,
    verified_at        INTEGER NOT NULL
);

-- Append-only history of every ACCEPTED checkpoint — the evidence and
-- replay plane. The records are themselves hash-chained and signed, so the
-- table needs no witness envelope; insertion order is adoption order (a
-- root-signed reset may re-anchor at a lower seq than a superseded fork,
-- so seq alone is not the key).
CREATE TABLE IF NOT EXISTS membership_checkpoints (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    org_uuid    TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    record      TEXT NOT NULL,
    accepted_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_membership_ckpt_org
    ON membership_checkpoints (org_uuid, id);
"""


@dataclass(frozen=True)
class OrgBinding:
    org_uuid: str
    root_pub: str
    recovery_policy: str
    recovery_pub: Optional[str]
    created_at: int
    expires_at: int
    renewed_at: Optional[int]
    endpoint_hints: Optional[list]
    policy_epoch: int = 0
    binding_generation: str = ""


@dataclass(frozen=True)
class MembershipState:
    """The registry's verified membership commitment for one org."""

    org_uuid: str
    seq: int
    members_root: str
    checkpointers_root: str
    ledger_head: str
    checkpoint: dict
    verified_at: int


def validate_membership_advance(
    stored_record: Optional[dict], record: object, root_pub: str
) -> None:
    """The one adoption rule, shared by the submission route and replay.

    Delegates record validation to
    :func:`tools.network.ledger.membership_commitment.validate_checkpoint`
    (schema, signatures, seq+1 linkage, the signer's inclusion proof under
    the previous ``checkpointers_root``), then adds the registry's two
    adoption rules: a member-signed record needs an adopted state to chain
    from, and a ROOT-SIGNED record must strictly advance the stored seq —
    "valid at any seq" (graph://da0dd9fb-e75) means seed-and-reset, not
    replaying an old captured reset to roll membership back.

    Raises ``MembershipCommitmentError`` naming the failed rule.
    """
    from tools.network.ledger.membership_commitment import (
        MembershipCommitmentError,
        validate_checkpoint,
    )

    if not isinstance(record, dict):
        raise MembershipCommitmentError("checkpoint must be a JSON object")
    root_signed = record.get("signer") == root_pub
    if root_signed:
        validate_checkpoint(record, root_pub=root_pub)
        if stored_record is not None and record.get("seq") <= stored_record["seq"]:
            raise MembershipCommitmentError(
                "a root-signed checkpoint must advance the stored seq — "
                "an old seed or reset cannot replay")
    else:
        if stored_record is None:
            raise MembershipCommitmentError(
                "no membership state for this org — a root-signed seed "
                "checkpoint must be adopted first")
        validate_checkpoint(record, root_pub=root_pub, prev_record=stored_record)


@dataclass(frozen=True)
class HostOwnership:
    reservation_id: str
    org: str
    persona_pub: str
    host: str
    generation: int
    created_at: int


@dataclass(frozen=True)
class LinkGrant:
    token: str
    org_uuid: str
    target_uuid: str
    target_type: str
    meta: dict
    created_at: int
    expires_at: Optional[int]
    revoked_at: Optional[int]
    signer_pub: str
    subject_kind: str
    subject_id: str
    invite_ref: Optional[str] = None
    expires_at_ms: Optional[int] = None
    operation_id: Optional[str] = None

    def is_expired_at(self, now_seconds: int) -> bool:
        if self.expires_at_ms is not None:
            return self.expires_at_ms < now_seconds * 1000
        return self.expires_at is not None and self.expires_at < now_seconds


@dataclass(frozen=True)
class LinkSession:
    session_id: str
    org_uuid: Optional[str]
    subject_kind: Optional[str]
    subject_id: Optional[str]
    dashboard_origin: Optional[str]
    identified: bool
    created_at: int
    expires_at: int


@dataclass(frozen=True)
class LinkChallenge:
    nonce: str
    session_id: str
    created_at: int
    expires_at: int
    redeemed_at: Optional[int]


@dataclass(frozen=True)
class LinkOperation:
    org_uuid: str
    operation_id: str
    operation: str
    binding_root_pub: str
    binding_generation: str
    receipt_request_digest: str
    acceptance_envelope_digest: str
    registry_input_digest: str
    local_intent_digest: str
    operand_digest: Optional[str]
    origin_proof_commitment: str
    source_expires_at_ms: Optional[int]
    accepted_at: int
    signer_pub: str
    subject_kind: str
    subject_id: str
    receipt_json: str
    receipt_signature: str
    state: str = "accepted"
    result_token: Optional[str] = None
    completed_at: Optional[int] = None


def _locked(method):
    """Serialize a store method under the instance lock.

    A single ``sqlite3.Connection`` is shared across threads
    (``check_same_thread=False``), and the ASGI server runs handlers in a
    threadpool — so without serialization two requests can drive the same
    connection/cursor at once and provoke SQLite API-misuse errors. Every
    method that touches the connection holds a reentrant lock for its whole
    body, so each method (a possibly multi-statement read-modify-write) is
    atomic against every other. The lock is reentrant so a locked method
    may call another locked method (e.g. ``publish_hint`` → ``latest_hint``)
    without deadlock.
    """

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


class RegistryStore:
    def __init__(self, db_path: str = ":memory:"):
        # RLock, not Lock: locked methods legitimately nest (see _locked).
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Idempotent additive migrations for pre-existing registry DBs —
        ``CREATE TABLE IF NOT EXISTS`` never adds a column to an existing
        table. Runs once at construction, before any concurrent access."""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(orgs)")}
        if "policy_epoch" not in cols:
            self._conn.execute(
                "ALTER TABLE orgs ADD COLUMN policy_epoch INTEGER NOT NULL DEFAULT 0"
            )
        if "binding_generation" not in cols:
            self._conn.execute("ALTER TABLE orgs ADD COLUMN binding_generation TEXT")
        for row in self._conn.execute(
            "SELECT org_uuid FROM orgs WHERE binding_generation IS NULL "
            "OR length(binding_generation) != 64 "
            "OR binding_generation GLOB '*[^0-9a-f]*'"
        ).fetchall():
            self._conn.execute(
                "UPDATE orgs SET binding_generation = ? WHERE org_uuid = ?",
                (secrets.token_hex(32), row["org_uuid"]),
            )
        challenge_cols = {
            r["name"] for r in self._conn.execute(
                "PRAGMA table_info(serve_challenges)")
        }
        if challenge_cols and "ttl" not in challenge_cols:
            self._conn.execute(
                "ALTER TABLE serve_challenges ADD COLUMN"
                " ttl INTEGER NOT NULL DEFAULT 60")
        if challenge_cols and "order_ref" not in challenge_cols:
            self._conn.execute(
                "ALTER TABLE serve_challenges ADD COLUMN"
                " order_ref TEXT NOT NULL DEFAULT ''")
        link_cols = {
            r["name"] for r in self._conn.execute("PRAGMA table_info(links)")
        }
        if "invite_ref" not in link_cols:
            self._conn.execute("ALTER TABLE links ADD COLUMN invite_ref TEXT")
        if "expires_at_ms" not in link_cols:
            self._conn.execute(
                "ALTER TABLE links ADD COLUMN expires_at_ms INTEGER"
            )
        if "operation_id" not in link_cols:
            self._conn.execute("ALTER TABLE links ADD COLUMN operation_id TEXT")
        # D19: org-tunnel grants carry no persona, so signer_pub/subject_id
        # must be nullable. A pre-D19 table has them NOT NULL; SQLite cannot
        # drop a column constraint in place, so rebuild the table when the
        # old constraint is present. Existing rows migrate unchanged.
        link_info = {
            r["name"]: r["notnull"]
            for r in self._conn.execute("PRAGMA table_info(links)")
        }
        if link_info and (link_info.get("signer_pub") or link_info.get("subject_id")):
            self._conn.executescript(
                """
                ALTER TABLE links RENAME TO links_pre_d19;
                CREATE TABLE links (
                    token        TEXT PRIMARY KEY,
                    org_uuid     TEXT NOT NULL,
                    target_uuid  TEXT NOT NULL,
                    target_type  TEXT NOT NULL,
                    invite_ref   TEXT,
                    meta         TEXT NOT NULL,
                    created_at   INTEGER NOT NULL,
                    expires_at   INTEGER,
                    expires_at_ms INTEGER,
                    revoked_at   INTEGER,
                    signer_pub   TEXT,
                    subject_kind TEXT NOT NULL,
                    subject_id   TEXT,
                    operation_id TEXT
                );
                INSERT INTO links (token, org_uuid, target_uuid, target_type,
                    invite_ref, meta, created_at, expires_at, expires_at_ms,
                    revoked_at, signer_pub, subject_kind, subject_id, operation_id)
                SELECT token, org_uuid, target_uuid, target_type,
                    invite_ref, meta, created_at, expires_at, expires_at_ms,
                    revoked_at, signer_pub, subject_kind, subject_id, operation_id
                FROM links_pre_d19;
                DROP TABLE links_pre_d19;
                CREATE INDEX IF NOT EXISTS idx_links_org ON links (org_uuid);
                """
            )
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_links_org_operation "
            "ON links (org_uuid, operation_id) WHERE operation_id IS NOT NULL"
        )

    @_locked
    def close(self) -> None:
        self._conn.close()

    # -- orgs ---------------------------------------------------------------

    @_locked
    def get_org(self, org_uuid: str) -> Optional[OrgBinding]:
        row = self._conn.execute("SELECT * FROM orgs WHERE org_uuid = ?", (org_uuid,)).fetchone()
        if row is None:
            return None
        return OrgBinding(
            org_uuid=row["org_uuid"],
            root_pub=row["root_pub"],
            recovery_policy=row["recovery_policy"],
            recovery_pub=row["recovery_pub"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            renewed_at=row["renewed_at"],
            endpoint_hints=json.loads(row["endpoint_hints"]) if row["endpoint_hints"] else None,
            policy_epoch=row["policy_epoch"],
            binding_generation=row["binding_generation"],
        )

    @_locked
    def create_org(
        self,
        org_uuid: str,
        root_pub: str,
        recovery_policy: str,
        recovery_pub: Optional[str],
        *,
        now: int,
        expires_at: int,
        endpoint_hints: Optional[list] = None,
        replacing_expired: bool = False,
    ) -> None:
        if replacing_expired:
            # Expired binding reclaimed: the old org's grants and
            # revocations died with its binding. Listings deliberately
            # survive — they belong to the publisher KEY-CONTINUITY, not
            # the UUID, so the reclaimer (a fresh continuity) cannot
            # extend, revoke, or re-occupy the old chains (L1).
            # Attestations are keyed by subject public key and never
            # touch org bindings at all.
            self._conn.execute("DELETE FROM orgs WHERE org_uuid = ?", (org_uuid,))
            self._conn.execute("DELETE FROM links WHERE org_uuid = ?", (org_uuid,))
            self._conn.execute("DELETE FROM revocations WHERE org_uuid = ?", (org_uuid,))
            self._conn.execute("DELETE FROM topic_hints WHERE org_uuid = ?", (org_uuid,))
            self._conn.execute("DELETE FROM topic_bundles WHERE org_uuid = ?", (org_uuid,))
            self._conn.execute("DELETE FROM witness_log WHERE org_uuid = ?", (org_uuid,))
            # Identity tied to the OLD binding dies with it: reclaimed UUID,
            # new root, no carried-over sessions, attributions, or nonces.
            self._conn.execute("DELETE FROM link_sessions WHERE org_uuid = ?", (org_uuid,))
            self._conn.execute("DELETE FROM consumed_assertions WHERE org_uuid = ?", (org_uuid,))
            self._conn.execute("DELETE FROM link_view_attributions WHERE org_uuid = ?", (org_uuid,))
            self._conn.execute("DELETE FROM link_operations WHERE org_uuid = ?", (org_uuid,))
        self._conn.execute(
            "INSERT INTO orgs (org_uuid, root_pub, recovery_policy, recovery_pub,"
            " created_at, expires_at, renewed_at, endpoint_hints, policy_epoch,"
            " binding_generation) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, 0, ?)",
            (
                org_uuid,
                root_pub,
                recovery_policy,
                recovery_pub,
                now,
                expires_at,
                json.dumps(endpoint_hints) if endpoint_hints is not None else None,
                secrets.token_hex(32),
            ),
        )
        self._conn.commit()

    @_locked
    def renew_org(self, org_uuid: str, *, now: int, expires_at: int) -> None:
        self._conn.execute(
            "UPDATE orgs SET expires_at = ?, renewed_at = ? WHERE org_uuid = ?",
            (expires_at, now, org_uuid),
        )
        self._conn.commit()

    @_locked
    def rebind_org(self, org_uuid: str, old_root_pub: str, new_root_pub: str, *, now: int) -> None:
        self._conn.execute(
            "UPDATE orgs SET root_pub = ? WHERE org_uuid = ?", (new_root_pub, org_uuid)
        )
        self._conn.execute(
            "INSERT INTO rebinds (org_uuid, old_root_pub, new_root_pub, rebound_at)"
            " VALUES (?, ?, ?, ?)",
            (org_uuid, old_root_pub, new_root_pub, now),
        )
        self._conn.commit()

    @_locked
    def update_recovery_policy(
        self,
        org_uuid: str,
        recovery_policy: str,
        recovery_pub: Optional[str],
        *,
        expected_epoch: int,
        new_epoch: int,
    ) -> bool:
        """Atomic compare-and-swap of the recovery policy on ``policy_epoch``
        (F1). The write only lands if the stored epoch still equals
        ``expected_epoch``; returns ``False`` on mismatch (a concurrent
        update or a stale/replayed request won the race). The epoch is the
        shared monotonic counter across policy-update and rebind, so this
        also serializes policy-vs-rebind."""
        cur = self._conn.execute(
            "UPDATE orgs SET recovery_policy = ?, recovery_pub = ?, policy_epoch = ? "
            "WHERE org_uuid = ? AND policy_epoch = ?",
            (recovery_policy, recovery_pub, new_epoch, org_uuid, expected_epoch),
        )
        self._conn.commit()
        return cur.rowcount > 0

    @_locked
    def claim_org(
        self,
        org_uuid: str,
        root_pub: str,
        recovery_policy: str,
        recovery_pub: Optional[str],
        *,
        now: int,
        expires_at: int,
        endpoint_hints: Optional[list] = None,
    ) -> str:
        """Atomic first-claim (F1/Codex): check existence + expiry and INSERT
        under ONE held lock, so two concurrent claims cannot both pass the
        existence check (the register_org get-then-create TOCTOU). Returns
        ``"already_bound_self"`` (the SAME root re-registering its own live
        binding — idempotent, liveness refreshed, no rebind),
        ``"conflict_live"`` (a DIFFERENT key wants a live binding — no
        write), ``"reclaimed_expired"`` (an expired binding was atomically
        replaced), or ``"claimed"`` (fresh). The RLock is held across the
        whole body, so get_org/create_org here are one atomic transaction."""
        existing = self.get_org(org_uuid)
        if existing is not None and existing.expires_at >= now:
            if existing.root_pub == root_pub:
                # Idempotent re-registration by the root that already holds
                # the binding: the caller proved control of exactly this key
                # (the envelope is self-signed by root_pub and verified), so
                # handing back "you already own this UUID" grants nothing new
                # — it is how a personal identity reliably recovers its own
                # org_uuid without minting a duplicate. Refresh liveness (a
                # self-signed re-registration is strictly stronger auth than
                # a renew heartbeat) but leave the bound root and recovery
                # policy untouched; changing the policy is /policy, rebinding
                # the root is /rebind. A DIFFERENT key on a live UUID still
                # conflicts below — names are not authority (§4.1).
                self.renew_org(org_uuid, now=now, expires_at=expires_at)
                return "already_bound_self"
            return "conflict_live"
        replacing = existing is not None
        self.create_org(
            org_uuid, root_pub, recovery_policy, recovery_pub,
            now=now, expires_at=expires_at, endpoint_hints=endpoint_hints,
            replacing_expired=replacing,
        )
        return "reclaimed_expired" if replacing else "claimed"

    @_locked
    def rebind_history(self, org_uuid: str) -> list:
        rows = self._conn.execute(
            "SELECT old_root_pub, new_root_pub, rebound_at FROM rebinds"
            " WHERE org_uuid = ? ORDER BY rebound_at",
            (org_uuid,),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- links --------------------------------------------------------------

    @_locked
    def get_link(self, token: str) -> Optional[LinkGrant]:
        row = self._conn.execute("SELECT * FROM links WHERE token = ?", (token,)).fetchone()
        if row is None:
            return None
        return LinkGrant(
            token=row["token"],
            org_uuid=row["org_uuid"],
            target_uuid=row["target_uuid"],
            target_type=row["target_type"],
            meta=json.loads(row["meta"]),
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            revoked_at=row["revoked_at"],
            signer_pub=row["signer_pub"],
            subject_kind=row["subject_kind"],
            subject_id=row["subject_id"],
            invite_ref=row["invite_ref"],
            expires_at_ms=row["expires_at_ms"],
            operation_id=row["operation_id"],
        )

    @_locked
    def create_link(self, grant: LinkGrant) -> None:
        self._conn.execute(
            "INSERT INTO links (token, org_uuid, target_uuid, target_type, invite_ref, meta,"
            " created_at, expires_at, expires_at_ms, revoked_at,"
            " signer_pub, subject_kind, subject_id, operation_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)",
            (
                grant.token,
                grant.org_uuid,
                grant.target_uuid,
                grant.target_type,
                grant.invite_ref,
                json.dumps(grant.meta),
                grant.created_at,
                grant.expires_at,
                grant.expires_at_ms,
                grant.signer_pub,
                grant.subject_kind,
                grant.subject_id,
                grant.operation_id,
            ),
        )
        self._conn.commit()

    @_locked
    def revoke_link(self, token: str, *, now: int) -> Optional[int]:
        self._conn.execute(
            "UPDATE links SET revoked_at = ? WHERE token = ? AND revoked_at IS NULL",
            (now, token),
        )
        row = self._conn.execute(
            "SELECT revoked_at FROM links WHERE token = ?", (token,)
        ).fetchone()
        self._conn.commit()
        return row["revoked_at"] if row is not None else None

    # -- Central Link operation receipts -----------------------------------

    @staticmethod
    def _operation_from_row(row: sqlite3.Row) -> LinkOperation:
        return LinkOperation(
            **{name: row[name] for name in LinkOperation.__dataclass_fields__}
        )

    @_locked
    def get_link_operation(
        self, org_uuid: str, operation_id: str
    ) -> Optional[LinkOperation]:
        row = self._conn.execute(
            "SELECT * FROM link_operations WHERE org_uuid = ? AND operation_id = ?",
            (org_uuid, operation_id),
        ).fetchone()
        return self._operation_from_row(row) if row is not None else None

    @_locked
    def claim_link_operation(
        self,
        candidate: LinkOperation,
        *,
        now: int,
        trusted_now_ms: Callable[[], int],
    ) -> tuple[str, Optional[LinkOperation]]:
        """Atomically create or replay one immutable accepted operation.

        The first fresh authorized envelope is retained for audit. A later
        freshly authorized delivery may use another valid signer, but can only
        retrieve the original receipt when every semantic coordinate matches.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            existing = self.get_link_operation(
                candidate.org_uuid, candidate.operation_id
            )
            semantic_fields = (
                "operation",
                "binding_root_pub",
                "binding_generation",
                "receipt_request_digest",
                "registry_input_digest",
                "local_intent_digest",
                "operand_digest",
                "origin_proof_commitment",
                "source_expires_at_ms",
            )
            if existing is not None:
                exact = all(
                    getattr(existing, name) == getattr(candidate, name)
                    for name in semantic_fields
                )
                self._conn.commit()
                return ("replay", existing) if exact else ("conflict", existing)

            # The source deadline is part of the same transaction-level
            # first-claim decision as the absence check above.  Keeping this
            # here (rather than in the HTTP handler) means a receipt accepted
            # just before the deadline and an exact delivery arriving at the
            # boundary converge on either the stored receipt or one stable
            # refusal; there is no lookup/insert race.
            if (
                candidate.source_expires_at_ms is not None
                and candidate.source_expires_at_ms <= trusted_now_ms()
            ):
                self._conn.commit()
                return "source_expired", None

            binding = self.get_org(candidate.org_uuid)
            if (
                binding is None
                or binding.expires_at < now
                or binding.root_pub != candidate.binding_root_pub
                or binding.binding_generation != candidate.binding_generation
            ):
                self._conn.commit()
                return "binding_mismatch", None
            self._conn.execute(
                "INSERT INTO link_operations (org_uuid, operation_id, operation,"
                " binding_root_pub, binding_generation, receipt_request_digest,"
                " acceptance_envelope_digest, registry_input_digest, local_intent_digest,"
                " operand_digest, origin_proof_commitment, source_expires_at_ms,"
                " accepted_at, signer_pub, subject_kind, subject_id, receipt_json,"
                " receipt_signature, state, result_token, completed_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
                " 'accepted', NULL, NULL)",
                (
                    candidate.org_uuid,
                    candidate.operation_id,
                    candidate.operation,
                    candidate.binding_root_pub,
                    candidate.binding_generation,
                    candidate.receipt_request_digest,
                    candidate.acceptance_envelope_digest,
                    candidate.registry_input_digest,
                    candidate.local_intent_digest,
                    candidate.operand_digest,
                    candidate.origin_proof_commitment,
                    candidate.source_expires_at_ms,
                    candidate.accepted_at,
                    candidate.signer_pub,
                    candidate.subject_kind,
                    candidate.subject_id,
                    candidate.receipt_json,
                    candidate.receipt_signature,
                ),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return "created", self.get_link_operation(
            candidate.org_uuid, candidate.operation_id
        )

    def _operation_binding_is_live(self, operation: LinkOperation, now: int) -> bool:
        binding = self.get_org(operation.org_uuid)
        return bool(
            binding is not None
            and binding.expires_at >= now
            and binding.root_pub == operation.binding_root_pub
            and binding.binding_generation == operation.binding_generation
        )

    @_locked
    def execute_publish_operation(
        self,
        org_uuid: str,
        operation_id: str,
        grant: LinkGrant,
        *,
        now: int,
    ) -> tuple[str, Optional[LinkOperation]]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            operation = self.get_link_operation(org_uuid, operation_id)
            if operation is None or operation.operation != "publish":
                self._conn.commit()
                return "not_found", operation
            if operation.state == "succeeded":
                link = self.get_link(operation.result_token or "")
                self._conn.commit()
                if (
                    link is None
                    or link.org_uuid != org_uuid
                    or link.operation_id != operation_id
                    or link.token != operation.result_token
                ):
                    return "inconsistent", operation
                return "replay", operation
            if operation.state != "accepted":
                self._conn.commit()
                return "conflict", operation
            if not self._operation_binding_is_live(operation, now):
                self._conn.commit()
                return "binding_mismatch", operation
            if grant.org_uuid != org_uuid or grant.operation_id != operation_id:
                self._conn.commit()
                return "conflict", operation
            self._conn.execute(
                "INSERT INTO links (token, org_uuid, target_uuid, target_type,"
                " invite_ref, meta, created_at, expires_at, expires_at_ms,"
                " revoked_at, signer_pub, subject_kind, subject_id, operation_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)",
                (
                    grant.token,
                    grant.org_uuid,
                    grant.target_uuid,
                    grant.target_type,
                    grant.invite_ref,
                    json.dumps(grant.meta),
                    grant.created_at,
                    grant.expires_at,
                    grant.expires_at_ms,
                    grant.signer_pub,
                    grant.subject_kind,
                    grant.subject_id,
                    grant.operation_id,
                ),
            )
            self._conn.execute(
                "UPDATE link_operations SET state = 'succeeded', result_token = ?,"
                " completed_at = ? WHERE org_uuid = ? AND operation_id = ?"
                " AND state = 'accepted'",
                (grant.token, now, org_uuid, operation_id),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return "created", self.get_link_operation(org_uuid, operation_id)

    @_locked
    def execute_revoke_operation(
        self,
        org_uuid: str,
        operation_id: str,
        token: str,
        *,
        now: int,
    ) -> tuple[str, Optional[LinkOperation]]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            operation = self.get_link_operation(org_uuid, operation_id)
            if operation is None or operation.operation != "revoke":
                self._conn.commit()
                return "not_found", operation
            if operation.state in ("succeeded", "not_found"):
                self._conn.commit()
                return "replay", operation
            if operation.state != "accepted":
                self._conn.commit()
                return "conflict", operation
            if not self._operation_binding_is_live(operation, now):
                self._conn.commit()
                return "binding_mismatch", operation
            link = self.get_link(token)
            if link is None or link.org_uuid != org_uuid:
                self._conn.execute(
                    "UPDATE link_operations SET state = 'not_found', completed_at = ?"
                    " WHERE org_uuid = ? AND operation_id = ?"
                    " AND state = 'accepted'",
                    (now, org_uuid, operation_id),
                )
                self._conn.commit()
                return "created", self.get_link_operation(org_uuid, operation_id)
            self._conn.execute(
                "UPDATE links SET revoked_at = ? WHERE token = ? AND revoked_at IS NULL",
                (now, token),
            )
            row = self._conn.execute(
                "SELECT revoked_at FROM links WHERE token = ?", (token,)
            ).fetchone()
            revoked_at = row["revoked_at"]
            self._conn.execute(
                "UPDATE link_operations SET state = 'succeeded', completed_at = ?"
                " WHERE org_uuid = ? AND operation_id = ? AND state = 'accepted'",
                (revoked_at, org_uuid, operation_id),
            )
            self._conn.commit()
            return "created", self.get_link_operation(org_uuid, operation_id)
        except Exception:
            self._conn.rollback()
            raise

    # -- revocations ---------------------------------------------------------

    @_locked
    def add_revocation(self, org_uuid: str, record: RevocationRecord) -> None:
        """Store a *verified* record; keep the longer-lived one on conflict."""
        existing = self._conn.execute(
            "SELECT expires_at FROM revocations WHERE org_uuid = ? AND revoked_key_id = ?",
            (org_uuid, record.revoked_key_id),
        ).fetchone()
        if existing is not None and existing["expires_at"] >= record.expires_at:
            return
        self._conn.execute(
            "INSERT INTO revocations (org_uuid, revoked_key_id, record, expires_at)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT (org_uuid, revoked_key_id) DO UPDATE SET"
            " record = excluded.record, expires_at = excluded.expires_at",
            (
                org_uuid,
                record.revoked_key_id,
                record.to_json().decode("ascii"),
                record.expires_at,
            ),
        )
        self._conn.commit()

    @_locked
    def revocation_set(self, org_uuid: str) -> RevocationSet:
        """The org's live denylist, as the object ``verify_chain`` consumes."""
        rows = self._conn.execute(
            "SELECT record FROM revocations WHERE org_uuid = ?", (org_uuid,)
        ).fetchall()
        rset = RevocationSet()
        for row in rows:
            rset.add(RevocationRecord.from_json(row["record"]))
        return rset

    @_locked
    def get_revocation(self, org_uuid: str, revoked_key_id: str) -> Optional[RevocationRecord]:
        row = self._conn.execute(
            "SELECT record FROM revocations WHERE org_uuid = ? AND revoked_key_id = ?",
            (org_uuid, revoked_key_id),
        ).fetchone()
        return RevocationRecord.from_json(row["record"]) if row else None

    @_locked
    def purge_expired_revocations(self, *, now: int) -> int:
        """I7 sweep: drop records past the revoked key's natural expiry."""
        cur = self._conn.execute("DELETE FROM revocations WHERE expires_at < ?", (now,))
        self._conn.commit()
        return cur.rowcount

    # -- topics (F3 broker path — hints + encrypted mailbox, L6) ---------------

    @_locked
    def publish_hint(
        self, org_uuid: str, topic: str, heads: list, signer_pub: str, *, now: int
    ) -> int:
        """Append a head announcement (hashes only); returns its seq.

        Re-announcing the head set already at the tip is a no-op that
        returns the existing seq — periodic heartbeat pushes from a quiet
        org must not grow the hint stream.
        """
        latest = self.latest_hint(org_uuid, topic)
        if latest is not None and latest["heads"] == heads:
            return latest["seq"]
        # seq allocation is a single INSERT..SELECT so concurrent writers
        # cannot both observe the same MAX and collide on the PK
        row = self._conn.execute(
            "INSERT INTO topic_hints (org_uuid, topic, seq, heads, signer_pub, published_at)"
            " SELECT ?, ?, COALESCE(MAX(seq), 0) + 1, ?, ?, ?"
            " FROM topic_hints WHERE org_uuid = ? AND topic = ?"
            " RETURNING seq",
            (org_uuid, topic, json.dumps(heads), signer_pub, now, org_uuid, topic),
        ).fetchone()
        self._conn.commit()
        return row[0]

    @_locked
    def hints_since(self, org_uuid: str, topic: str, since: int, limit: int = 256) -> list:
        rows = self._conn.execute(
            "SELECT seq, heads, published_at FROM topic_hints"
            " WHERE org_uuid = ? AND topic = ? AND seq > ? ORDER BY seq LIMIT ?",
            (org_uuid, topic, since, limit),
        ).fetchall()
        return [
            {"seq": r["seq"], "heads": json.loads(r["heads"]), "published_at": r["published_at"]}
            for r in rows
        ]

    @_locked
    def latest_hint(self, org_uuid: str, topic: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT seq, heads, published_at FROM topic_hints"
            " WHERE org_uuid = ? AND topic = ? ORDER BY seq DESC LIMIT 1",
            (org_uuid, topic),
        ).fetchone()
        if row is None:
            return None
        return {"seq": row["seq"], "heads": json.loads(row["heads"]), "published_at": row["published_at"]}

    # -- node reachability hints (G1 fabric path, spec §8) ---------------------

    @_locked
    def upsert_node_hint(
        self,
        org_uuid: str,
        node_pub: str,
        addrs: list,
        relay_url: Optional[str],
        *,
        now: int,
        expires_at: int,
    ) -> None:
        """Announce/refresh one node's reachability. Last write wins —
        a refresh replaces the previous candidate set entirely, so an
        address the node stopped announcing disappears immediately rather
        than lingering until TTL."""
        self._conn.execute(
            "INSERT INTO node_hints (org_uuid, node_pub, addrs, relay_url,"
            " announced_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (org_uuid, node_pub) DO UPDATE SET"
            " addrs = excluded.addrs, relay_url = excluded.relay_url,"
            " announced_at = excluded.announced_at, expires_at = excluded.expires_at",
            (org_uuid, node_pub, json.dumps(addrs), relay_url, now, expires_at),
        )
        self._conn.commit()

    @_locked
    def node_hints(
        self, org_uuid: str, *, now: int, node_pub: Optional[str] = None
    ) -> list:
        """Live (non-expired) hints for an org, optionally one node's."""
        query = (
            "SELECT node_pub, addrs, relay_url, announced_at, expires_at"
            " FROM node_hints WHERE org_uuid = ? AND expires_at >= ?"
        )
        params: tuple = (org_uuid, now)
        if node_pub is not None:
            query += " AND node_pub = ?"
            params += (node_pub,)
        rows = self._conn.execute(query + " ORDER BY node_pub", params).fetchall()
        return [
            {
                "node": r["node_pub"],
                "addrs": json.loads(r["addrs"]),
                "relay_url": r["relay_url"],
                "announced_at": r["announced_at"],
                "expires_at": r["expires_at"],
            }
            for r in rows
        ]

    @_locked
    def purge_expired_node_hints(self, *, now: int) -> int:
        cur = self._conn.execute("DELETE FROM node_hints WHERE expires_at < ?", (now,))
        self._conn.commit()
        return cur.rowcount

    # -- serving hostname ownership (auto-0zdky) ---------------------------

    def _host_ownership_row(self, row) -> Optional["HostOwnership"]:
        if row is None:
            return None
        return HostOwnership(
            reservation_id=row["reservation_id"],
            org=row["org_uuid"],
            persona_pub=row["persona_pub"],
            host=row["host"],
            generation=row["generation"],
            created_at=row["created_at"],
        )

    @_locked
    def get_host_ownership(
        self, reservation_id: str
    ) -> Optional["HostOwnership"]:
        row = self._conn.execute(
            "SELECT reservation_id, org_uuid, persona_pub, host, generation,"
            " created_at FROM serve_hosts WHERE reservation_id = ?",
            (reservation_id,),
        ).fetchone()
        return self._host_ownership_row(row)

    @_locked
    def get_host_ownership_by_host(
        self, host: str
    ) -> Optional["HostOwnership"]:
        row = self._conn.execute(
            "SELECT reservation_id, org_uuid, persona_pub, host, generation,"
            " created_at FROM serve_hosts WHERE host = ?",
            (host,),
        ).fetchone()
        return self._host_ownership_row(row)

    @_locked
    def upsert_serve_challenge(
        self, name: str, value: str, *, expires_at: int, now: int,
        ttl: int = 60, order_ref: str = "", max_values: int = 8,
    ) -> None:
        """Add/refresh one challenge value; purges expired rows first and
        bounds live values per name (raises ValueError at the cap)."""
        self._conn.execute(
            "DELETE FROM serve_challenges WHERE expires_at < ?", (now,))
        live = self._conn.execute(
            "SELECT COUNT(*) AS n FROM serve_challenges"
            " WHERE name = ? AND value != ?",
            (name, value),
        ).fetchone()["n"]
        if live >= max_values:
            self._conn.rollback()
            raise ValueError(f"{max_values} live values at {name}")
        self._conn.execute(
            "INSERT INTO serve_challenges"
            " (name, value, expires_at, ttl, order_ref)"
            " VALUES (?, ?, ?, ?, ?) ON CONFLICT (name, value)"
            " DO UPDATE SET expires_at = excluded.expires_at,"
            " ttl = excluded.ttl, order_ref = excluded.order_ref",
            (name, value, expires_at, ttl, order_ref),
        )
        self._conn.commit()

    @_locked
    def delete_serve_challenge(
        self, name: str, value: str, *, order_ref: str | None = None,
    ) -> None:
        """Remove one value; with *order_ref*, only the owning order's
        row (a mismatched order removes nothing — bhs3c cleanup scope)."""
        if order_ref is None:
            self._conn.execute(
                "DELETE FROM serve_challenges WHERE name = ? AND value = ?",
                (name, value),
            )
        else:
            self._conn.execute(
                "DELETE FROM serve_challenges WHERE name = ? AND value = ?"
                " AND order_ref = ?",
                (name, value, order_ref),
            )
        self._conn.commit()

    @_locked
    def live_serve_challenges(self, *, now: int) -> dict:
        """{fqdn: {"values": [...], "ttl": min}} of unexpired challenges,
        purging inline. The per-name TTL is the minimum across values."""
        self._conn.execute(
            "DELETE FROM serve_challenges WHERE expires_at < ?", (now,))
        self._conn.commit()
        rows = self._conn.execute(
            "SELECT name, value, ttl FROM serve_challenges"
            " ORDER BY name, value",
        ).fetchall()
        challenges: dict = {}
        for row in rows:
            entry = challenges.setdefault(
                row["name"], {"values": [], "ttl": row["ttl"]})
            entry["values"].append(row["value"])
            entry["ttl"] = min(entry["ttl"], row["ttl"])
        return challenges

    @_locked
    def get_persona_label(self, persona_pub: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT label FROM serve_labels WHERE persona_pub = ?",
            (persona_pub,),
        ).fetchone()
        return row["label"] if row is not None else None

    def registered_serving_keys(self, org: str) -> set:
        """The org's allow-set of registered serving machine pubkeys
        (auto-e2ufw). Empty until onboarding/backfill registers them."""
        rows = self._conn.execute(
            "SELECT machine_pub FROM serve_machine_keys WHERE org_uuid = ?",
            (org,),
        ).fetchall()
        return {row["machine_pub"] for row in rows}

    def count_orgs_with_serving_keys(self) -> int:
        """How many orgs have at least one registered serving key — the
        complement of the un-backfilled set, for the transitional metric."""
        row = self._conn.execute(
            "SELECT COUNT(DISTINCT org_uuid) AS n FROM serve_machine_keys"
        ).fetchone()
        return int(row["n"]) if row is not None else 0

    @_locked
    def register_serving_machine_key(
        self, org: str, machine_pub: str, *, now: int
    ) -> None:
        """Idempotently add a serving machine pubkey to an org's allow-set
        (onboarding and host-terminal backfill). Re-registration is a
        no-op; it never re-keys or removes anything."""
        self._conn.execute(
            "INSERT OR IGNORE INTO serve_machine_keys"
            " (org_uuid, machine_pub, created_at) VALUES (?, ?, ?)",
            (org, machine_pub, now),
        )
        self._conn.commit()

    @_locked
    def bind_persona_label(
        self, persona_pub: str, label: str, *, now: int
    ) -> bool:
        """Bind a persona's immutable serving label on first registration.
        Returns False when the label is already bound to a different
        persona (refused, never renamed)."""
        try:
            self._conn.execute(
                "INSERT INTO serve_labels (persona_pub, label, created_at)"
                " VALUES (?, ?, ?)",
                (persona_pub, label, now),
            )
            self._conn.commit()
            return True
        except sqlite3.IntegrityError:
            self._conn.rollback()
            return False

    @_locked
    def upsert_host_ownership(
        self,
        *,
        reservation_id: str,
        org: str,
        persona_pub: str,
        host: str,
        now: int,
    ) -> int:
        """Record/refresh ownership and advance the lease generation.

        Callers validate persona binding and conflicts BEFORE this write;
        the method itself only enforces row identity. Returns the new
        generation — monotonic across restarts by construction."""
        self._conn.execute(
            "INSERT INTO serve_hosts (reservation_id, org_uuid, persona_pub,"
            " host, generation, created_at) VALUES (?, ?, ?, ?, 1, ?)"
            " ON CONFLICT (reservation_id) DO UPDATE SET"
            " generation = serve_hosts.generation + 1",
            (reservation_id, org, persona_pub, host, now),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT generation FROM serve_hosts WHERE reservation_id = ?",
            (reservation_id,),
        ).fetchone()
        return int(row["generation"])

    @_locked
    def deposit_bundle(
        self,
        org_uuid: str,
        topic: str,
        v: int,
        hashes: list,
        ciphertext: bytes,
        signer_pub: str,
        *,
        now: int,
    ) -> int:
        """Store-and-forward one ENCRYPTED bundle; returns its seq.

        The row is the whole L6 disclosure: bundle version, topic,
        hashes, size, and an opaque blob. The store never sees (and
        cannot check) plaintext. ``v`` is stored verbatim so a future
        format bump can still open (or deliberately migrate) old rows.
        """
        row = self._conn.execute(
            "INSERT INTO topic_bundles (org_uuid, topic, seq, v, hashes, size, ciphertext,"
            " signer_pub, deposited_at)"
            " SELECT ?, ?, COALESCE(MAX(seq), 0) + 1, ?, ?, ?, ?, ?, ?"
            " FROM topic_bundles WHERE org_uuid = ? AND topic = ?"
            " RETURNING seq",
            (org_uuid, topic, v, json.dumps(hashes), len(ciphertext), ciphertext,
             signer_pub, now, org_uuid, topic),
        ).fetchone()
        self._conn.commit()
        return row[0]

    @_locked
    def bundles_since(
        self,
        org_uuid: str,
        topic: str,
        since: int,
        limit: int = 64,
        *,
        meta_only: bool = False,
    ) -> list:
        columns = "seq, v, hashes, size" + ("" if meta_only else ", ciphertext")
        rows = self._conn.execute(
            f"SELECT {columns} FROM topic_bundles"
            " WHERE org_uuid = ? AND topic = ? AND seq > ? ORDER BY seq LIMIT ?",
            (org_uuid, topic, since, limit),
        ).fetchall()
        out = []
        for r in rows:
            entry = {"seq": r["seq"], "v": r["v"], "hashes": json.loads(r["hashes"]),
                     "size": r["size"]}
            if not meta_only:
                entry["ciphertext"] = bytes(r["ciphertext"])
            out.append(entry)
        return out

    @_locked
    def bundles_with(self, org_uuid: str, topic: str, want: list, limit: int = 64) -> list:
        """Fetch-missing-by-hash: bundles containing any wanted event id.

        Two-phase so the manifest scan never materializes ciphertext:
        blob pages are read only for the (few) matching rows.
        """
        wanted = set(want)
        matched = []
        for row in self._conn.execute(
            "SELECT seq, hashes FROM topic_bundles"
            " WHERE org_uuid = ? AND topic = ? ORDER BY seq",
            (org_uuid, topic),
        ):
            if wanted & set(json.loads(row["hashes"])):
                matched.append(row["seq"])
                if len(matched) >= limit:
                    break
        out = []
        for seq in matched:
            r = self._conn.execute(
                "SELECT seq, v, hashes, size, ciphertext FROM topic_bundles"
                " WHERE org_uuid = ? AND topic = ? AND seq = ?",
                (org_uuid, topic, seq),
            ).fetchone()
            out.append(
                {
                    "seq": r["seq"],
                    "v": r["v"],
                    "hashes": json.loads(r["hashes"]),
                    "size": r["size"],
                    "ciphertext": bytes(r["ciphertext"]),
                }
            )
        return out

    # -- listings (L1 listing directory — signed cards + attestations) ----------

    @staticmethod
    def _listing_row(row) -> dict:
        return {
            "publisher": row["publisher"],
            "name": row["name"],
            "seq": row["seq"],
            "listing_id": row["listing_id"],
            "prev_id": row["prev_id"],
            "version": row["version"],
            "bundle_hash": row["bundle_hash"],
            "claim": row["claim"],
            "root_pub": row["root_pub"],
            "created_at": row["created_at"],
            "revoked_at": row["revoked_at"],
        }

    def get_listing(self, listing_id: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM listings WHERE listing_id = ?", (listing_id,)
        ).fetchone()
        return self._listing_row(row) if row is not None else None

    def listing_head(self, publisher: str, name: str) -> Optional[dict]:
        """The newest row for ``(publisher, name)`` — revoked or not."""
        row = self._conn.execute(
            "SELECT * FROM listings WHERE publisher = ? AND name = ?"
            " ORDER BY seq DESC LIMIT 1",
            (publisher, name),
        ).fetchone()
        return self._listing_row(row) if row is not None else None

    def insert_listing(
        self,
        *,
        publisher: str,
        name: str,
        listing_id: str,
        prev_id: Optional[str],
        version: str,
        bundle_hash: str,
        claim: str,
        root_pub: str,
        signer_pub: str,
        subject_kind: str,
        subject_id: str,
        now: int,
    ) -> int:
        """Append one accepted listing claim; returns its chain seq.

        ``root_pub`` is the publisher org's bound root AT ACCEPT TIME —
        the anchor the key-continuity update rule checks against. seq
        allocation is a single INSERT..SELECT so concurrent writers
        cannot both observe the same MAX and collide on the PK.
        """
        row = self._conn.execute(
            "INSERT INTO listings (publisher, name, seq, listing_id, prev_id,"
            " version, bundle_hash, claim, root_pub, signer_pub, subject_kind,"
            " subject_id, created_at, revoked_at)"
            " SELECT ?, ?, COALESCE(MAX(seq), 0) + 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL"
            " FROM listings WHERE publisher = ? AND name = ?"
            " RETURNING seq",
            (publisher, name, listing_id, prev_id, version, bundle_hash, claim,
             root_pub, signer_pub, subject_kind, subject_id, now,
             publisher, name),
        ).fetchone()
        self._conn.commit()
        return row[0]

    def listing_history(self, publisher: str, name: str) -> list:
        """Chain metadata, oldest first — deliberately WITHOUT the claim
        column, so a long chain of icon-heavy cards stays cheap to list;
        any single card is fetched by its head/id."""
        rows = self._conn.execute(
            "SELECT listing_id, seq, prev_id, version, bundle_hash, root_pub,"
            " created_at, revoked_at"
            " FROM listings WHERE publisher = ? AND name = ? ORDER BY seq",
            (publisher, name),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_active_listings(
        self, *, publisher: Optional[str] = None, name: Optional[str] = None,
        limit: int = 256,
    ) -> list:
        """Directory view: the unrevoked head of every listing chain.

        Names are labels, not property — a name filter can return heads
        from several publishers, none privileged over the others.
        """
        query = (
            "SELECT * FROM listings AS l WHERE l.revoked_at IS NULL"
            " AND l.seq = (SELECT MAX(seq) FROM listings"
            "              WHERE publisher = l.publisher AND name = l.name)"
        )
        args: list = []
        if publisher is not None:
            query += " AND l.publisher = ?"
            args.append(publisher)
        if name is not None:
            query += " AND l.name = ?"
            args.append(name)
        query += " ORDER BY l.publisher, l.name LIMIT ?"
        args.append(limit)
        return [self._listing_row(r) for r in self._conn.execute(query, args)]

    def revoke_listing(self, publisher: str, name: str, *, now: int) -> int:
        """Mark every unrevoked row of the chain revoked; returns count."""
        cur = self._conn.execute(
            "UPDATE listings SET revoked_at = ?"
            " WHERE publisher = ? AND name = ? AND revoked_at IS NULL",
            (now, publisher, name),
        )
        self._conn.commit()
        return cur.rowcount

    def root_continuity(self, org_uuid: str, current_root: str) -> set:
        """Roots connected to *current_root* through the org's rebind trail.

        The set of keys the CURRENT binding is a legitimate successor of:
        walk the ``rebinds`` audit edges backwards from the current root.
        A root that got the UUID by expiry-reclaim has no edge into this
        set, which is exactly what "account is never authority" means for
        listing updates.
        """
        predecessors: dict = {}
        for edge in self._conn.execute(
            "SELECT old_root_pub, new_root_pub FROM rebinds WHERE org_uuid = ?",
            (org_uuid,),
        ):
            predecessors.setdefault(edge["new_root_pub"], []).append(edge["old_root_pub"])
        # BFS over the reverse edges — linear in the rebind count, which an
        # org can grow one signed rebind at a time.
        continuity = {current_root}
        frontier = [current_root]
        while frontier:
            for old_root in predecessors.get(frontier.pop(), ()):
                if old_root not in continuity:
                    continuity.add(old_root)
                    frontier.append(old_root)
        return continuity

    # -- attestations ------------------------------------------------------------

    def add_attestation(
        self, attestation_id: str, payload: dict, record: str, *, now: int
    ) -> None:
        """Store a signature-verified attestation record.

        Keyed by the logical claim; a record with a newer ``ts`` replaces
        the row, an older-or-equal one is a no-op (redelivery of the
        identical record therefore idempotent) — so a replayed stale
        attestation can never roll a refreshed claim back.
        """
        existing = self._conn.execute(
            "SELECT ts FROM attestations WHERE attestor_pub = ? AND subject_pub = ?"
            " AND claim_type = ? AND claim_value = ?",
            (payload["attestor"], payload["subject"],
             payload["claim_type"], payload["claim_value"]),
        ).fetchone()
        if existing is not None and existing["ts"] >= payload["ts"]:
            return
        self._conn.execute(
            "INSERT INTO attestations (attestor_pub, subject_pub, claim_type,"
            " claim_value, attestation_id, record, ts, expires_at, received_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (attestor_pub, subject_pub, claim_type, claim_value)"
            " DO UPDATE SET attestation_id = excluded.attestation_id,"
            " record = excluded.record, ts = excluded.ts,"
            " expires_at = excluded.expires_at, received_at = excluded.received_at",
            (payload["attestor"], payload["subject"], payload["claim_type"],
             payload["claim_value"], attestation_id, record,
             payload["ts"], payload["ts"] + payload["ttl"], now),
        )
        self._conn.commit()

    def attestations_for_subject(
        self, subject_pub: str, *, now: int, limit: int = 256
    ) -> list:
        """Live (unexpired) attestation records about *subject_pub*,
        freshest first (the useful end when the page cap bites)."""
        rows = self._conn.execute(
            "SELECT * FROM attestations WHERE subject_pub = ? AND expires_at > ?"
            " ORDER BY ts DESC, attestation_id LIMIT ?",
            (subject_pub, now, limit),
        ).fetchall()
        return [
            {
                "attestation_id": r["attestation_id"],
                "attestor": r["attestor_pub"],
                "subject": r["subject_pub"],
                "claim_type": r["claim_type"],
                "claim_value": r["claim_value"],
                "record": r["record"],
                "ts": r["ts"],
                "expires_at": r["expires_at"],
            }
            for r in rows
        ]

    def purge_expired_attestations(self, *, now: int) -> int:
        """TTL sweep: drop records past their declared expiry."""
        cur = self._conn.execute("DELETE FROM attestations WHERE expires_at <= ?", (now,))
        self._conn.commit()
        return cur.rowcount

    # -- witness (F4 equivocation witness — append-only head-set log, L5) -------

    def _witness_row(self, row) -> dict:
        """A stored row as ``{entry, entry_id}`` — the shape the app signs."""
        return {
            "entry": {
                "org": row["org_uuid"],
                "topic": row["topic"],
                "seq": row["seq"],
                "heads": json.loads(row["heads"]),
                "prev": row["prev_id"],
                "publisher": row["publisher"],
            },
            "entry_id": row["entry_id"],
        }

    # -- committed membership (graph://da0dd9fb-e75, auto-1wxet) ---------------

    @_locked
    def get_membership_state(self, org_uuid: str) -> Optional[MembershipState]:
        row = self._conn.execute(
            "SELECT * FROM membership_state WHERE org_uuid = ?", (org_uuid,)
        ).fetchone()
        if row is None:
            return None
        return MembershipState(
            org_uuid=row["org_uuid"],
            seq=row["seq"],
            members_root=row["members_root"],
            checkpointers_root=row["checkpointers_root"],
            ledger_head=row["ledger_head"],
            checkpoint=json.loads(row["checkpoint"]),
            verified_at=row["verified_at"],
        )

    @_locked
    def advance_membership_state(self, org_uuid: str, record: dict, *, now: int) -> None:
        """Adopt an ALREADY-VALIDATED checkpoint: upsert the verified tuple
        and append the evidence row in one transaction. Callers run
        :func:`validate_membership_advance` first — this method persists,
        it does not judge."""
        self._conn.execute(
            "INSERT INTO membership_state (org_uuid, seq, members_root,"
            " checkpointers_root, ledger_head, checkpoint, verified_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (org_uuid) DO UPDATE SET seq=excluded.seq,"
            " members_root=excluded.members_root,"
            " checkpointers_root=excluded.checkpointers_root,"
            " ledger_head=excluded.ledger_head,"
            " checkpoint=excluded.checkpoint,"
            " verified_at=excluded.verified_at",
            (org_uuid, record["seq"], record["members_root"],
             record["checkpointers_root"], record["ledger_head"],
             json.dumps(record, sort_keys=True, separators=(",", ":")), now),
        )
        self._conn.execute(
            "INSERT INTO membership_checkpoints (org_uuid, seq, record,"
            " accepted_at) VALUES (?, ?, ?, ?)",
            (org_uuid, record["seq"],
             json.dumps(record, sort_keys=True, separators=(",", ":")), now),
        )
        self._conn.commit()

    @_locked
    def membership_checkpoint_history(self, org_uuid: str, limit: int = 100_000) -> list:
        """Every accepted checkpoint record, in adoption order — the replay
        source :meth:`rebuild_membership_state` consumes."""
        rows = self._conn.execute(
            "SELECT record FROM membership_checkpoints WHERE org_uuid = ?"
            " ORDER BY id LIMIT ?",
            (org_uuid, limit),
        ).fetchall()
        return [json.loads(r["record"]) for r in rows]

    def rebuild_membership_state(self, org_uuid: str, root_pub: str) -> Optional[dict]:
        """Replay the accepted-checkpoint history through the SAME adoption
        rule the submission route runs, returning the final record — the
        deploy runbook's recovery check: the result must equal the stored
        ``membership_state`` checkpoint exactly. Raises on a history that no
        longer validates (evidence of tampering, not a recoverable state).
        Read-only: it never writes."""
        state: Optional[dict] = None
        for record in self.membership_checkpoint_history(org_uuid):
            validate_membership_advance(state, record, root_pub)
            state = record
        return state

    @_locked
    def witness_tip(self, org_uuid: str, topic: str) -> Optional[dict]:
        """The current head-set attestation entry, or ``None`` if empty."""
        row = self._conn.execute(
            "SELECT * FROM witness_log WHERE org_uuid = ? AND topic = ?"
            " ORDER BY seq DESC LIMIT 1",
            (org_uuid, topic),
        ).fetchone()
        return self._witness_row(row) if row is not None else None

    @_locked
    def append_witness(
        self, org_uuid: str, topic: str, heads: list, publisher: str, *, now: int
    ) -> dict:
        """Append one head-set to the chain; returns its ``{entry, entry_id}``.

        The new entry is chained to the current tip (``prev`` = tip's
        content address, ``seq`` = tip seq + 1), which is what makes the
        served log tamper-evident: a rewrite of any past entry breaks the
        hash chain the members already hold signed. Re-attesting the head
        set already at the tip is idempotent — it returns the tip unchanged
        so a quiet org's heartbeat never grows the log (matching the hint
        stream's dedup). The store is single-writer; the ``(org, topic,
        seq)`` primary key is the backstop that turns any lost race into a
        loud ``IntegrityError`` rather than a forked chain.
        """
        heads_sorted = sorted(set(heads))
        tip = self.witness_tip(org_uuid, topic)
        if tip is not None and tip["entry"]["heads"] == heads_sorted:
            return tip
        seq = 1 if tip is None else tip["entry"]["seq"] + 1
        prev = None if tip is None else tip["entry_id"]
        entry = build_entry(org_uuid, topic, seq, heads_sorted, prev, publisher)
        eid = _entry_id(entry)
        self._conn.execute(
            "INSERT INTO witness_log (org_uuid, topic, seq, heads, prev_id,"
            " entry_id, publisher, witnessed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (org_uuid, topic, seq, json.dumps(entry["heads"]), prev, eid, publisher, now),
        )
        self._conn.commit()
        return {"entry": entry, "entry_id": eid}

    @_locked
    def witness_since(
        self, org_uuid: str, topic: str, since: int, limit: int = 256
    ) -> list:
        """Chain entries with ``seq > since`` (for the client's chain walk)."""
        rows = self._conn.execute(
            "SELECT * FROM witness_log WHERE org_uuid = ? AND topic = ? AND seq > ?"
            " ORDER BY seq LIMIT ?",
            (org_uuid, topic, since, limit),
        ).fetchall()
        return [self._witness_row(r) for r in rows]

    # -- Witness v2: one grouped, timestamped chain per org (auto-jqd9q) --------

    def _witness_row_v2(self, row) -> dict:
        return {
            "entry": {
                "v": 2,
                "org": row["org_uuid"],
                "seq": row["seq"],
                "prev": row["prev_id"],
                "t": row["t"],
                "heads": json.loads(row["heads_json"]),
                "publisher": row["publisher"],
            },
            "entry_id": row["entry_id"],
        }

    @_locked
    def witness_tip_v2(self, org_uuid: str) -> Optional[dict]:
        """The org's current v2 tip attestation entry, or ``None`` if empty."""
        row = self._conn.execute(
            "SELECT * FROM witness_log_v2 WHERE org_uuid = ? ORDER BY seq DESC LIMIT 1",
            (org_uuid,),
        ).fetchone()
        return self._witness_row_v2(row) if row is not None else None

    def _migrate_v1_to_v2_if_needed(self, org_uuid: str, *, now: int) -> None:
        """One-time, at the first v2 append for an org: if the v2 chain is empty
        and v1 chains exist, seed ``seq 1`` from the v1 topic tips.

        Each v1 topic's tip head set maps to its v2 group — ``storage`` stays
        ``storage``, every other v1 topic (the ledger topic included) maps to
        ``authority``. The archived v1 chain is left in place and fetchable.
        Clients holding a v1 journal treat this first v2 attestation as a fresh
        baseline (the client's v1 re-anchor).
        """
        if self._conn.execute(
            "SELECT 1 FROM witness_log_v2 WHERE org_uuid = ? LIMIT 1", (org_uuid,)
        ).fetchone() is not None:
            return
        v1_topics = [
            r["topic"] for r in self._conn.execute(
                "SELECT DISTINCT topic FROM witness_log WHERE org_uuid = ?", (org_uuid,)
            ).fetchall()
        ]
        if not v1_topics:
            return
        heads_by_group: dict = {}
        for topic in v1_topics:
            tip = self.witness_tip(org_uuid, topic)
            if tip is None:
                continue
            group = "storage" if topic == "storage" else "authority"
            merged = set(heads_by_group.get(group, [])) | set(tip["entry"]["heads"])
            heads_by_group[group] = sorted(merged)
        if not heads_by_group:
            return
        entry = build_entry_v2(org_uuid, 1, heads_by_group, None, now, tip["entry"]["publisher"])
        eid = _entry_id(entry)
        self._conn.execute(
            "INSERT INTO witness_log_v2 (org_uuid, seq, prev_id, t, heads_json,"
            " entry_id, publisher, witnessed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (org_uuid, 1, None, now, json.dumps(entry["heads"]), eid,
             entry["publisher"], now),
        )
        self._conn.commit()

    @_locked
    def append_witness_v2(
        self, org_uuid: str, topic: str, heads: list, publisher: str, *, now: int
    ) -> dict:
        """Append one topic's head set into the org's single grouped chain.

        ``topic`` must be a member of :data:`WITNESS_TOPICS`. The stored entry
        always carries the FULL current head map — every topic seen so far,
        with this publication's topic replaced — so domination and re-serves are
        self-contained. Idempotent when the resulting map equals the tip's.
        ``t = max(now, tip.t)`` so the chain's timestamp is non-decreasing by
        construction and the server never emits a backdated entry. The
        ``(org, seq)`` primary key turns any lost single-writer race into a loud
        ``IntegrityError`` rather than a forked chain.
        """
        if topic not in WITNESS_TOPICS:
            raise ValueError(f"unknown witness topic {topic!r}")
        self._migrate_v1_to_v2_if_needed(org_uuid, now=now)
        tip = self.witness_tip_v2(org_uuid)
        current = dict(tip["entry"]["heads"]) if tip is not None else {}
        current[topic] = sorted(set(heads))
        if tip is not None and current == tip["entry"]["heads"]:
            return tip
        seq = 1 if tip is None else tip["entry"]["seq"] + 1
        prev = None if tip is None else tip["entry_id"]
        t = now if tip is None else max(now, tip["entry"]["t"])
        entry = build_entry_v2(org_uuid, seq, current, prev, t, publisher)
        eid = _entry_id(entry)
        self._conn.execute(
            "INSERT INTO witness_log_v2 (org_uuid, seq, prev_id, t, heads_json,"
            " entry_id, publisher, witnessed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (org_uuid, seq, prev, t, json.dumps(entry["heads"]), eid, publisher, now),
        )
        self._conn.commit()
        return {"entry": entry, "entry_id": eid}

    @_locked
    def witness_since_v2(self, org_uuid: str, since: int, limit: int = 256) -> list:
        """v2 chain entries with ``seq > since`` (for the client's chain walk)."""
        rows = self._conn.execute(
            "SELECT * FROM witness_log_v2 WHERE org_uuid = ? AND seq > ?"
            " ORDER BY seq LIMIT ?",
            (org_uuid, since, limit),
        ).fetchall()
        return [self._witness_row_v2(r) for r in rows]

    # -- E1 session linking (§4.7, §4.8, §6.8) ---------------------------------

    def _row_to_session(self, row) -> LinkSession:
        return LinkSession(
            session_id=row["session_id"],
            org_uuid=row["org_uuid"],
            subject_kind=row["subject_kind"],
            subject_id=row["subject_id"],
            dashboard_origin=row["dashboard_origin"],
            identified=bool(row["identified"]),
            created_at=row["created_at"],
            expires_at=row["expires_at"],
        )

    @_locked
    def get_session(self, session_id: str) -> Optional[LinkSession]:
        row = self._conn.execute(
            "SELECT * FROM link_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        return self._row_to_session(row) if row is not None else None

    @_locked
    def create_anonymous_session(self, session_id: str, *, now: int, expires_at: int) -> None:
        """Open a fresh anonymous session (identified=0, no identity)."""
        self._conn.execute(
            "INSERT INTO link_sessions (session_id, org_uuid, subject_kind, subject_id,"
            " dashboard_origin, identified, created_at, expires_at)"
            " VALUES (?, NULL, NULL, NULL, NULL, 0, ?, ?)",
            (session_id, now, expires_at),
        )
        self._conn.commit()

    @_locked
    def purge_expired_sessions(self, *, now: int) -> int:
        cur = self._conn.execute("DELETE FROM link_sessions WHERE expires_at < ?", (now,))
        self._conn.commit()
        return cur.rowcount

    # -- QR challenges ---------------------------------------------------------

    @_locked
    def create_challenge(
        self, nonce: str, session_id: str, *, now: int, expires_at: int
    ) -> None:
        self._conn.execute(
            "INSERT INTO link_challenges (nonce, session_id, created_at, expires_at, redeemed_at)"
            " VALUES (?, ?, ?, ?, NULL)",
            (nonce, session_id, now, expires_at),
        )
        self._conn.commit()

    @_locked
    def get_challenge(self, nonce: str) -> Optional[LinkChallenge]:
        row = self._conn.execute(
            "SELECT * FROM link_challenges WHERE nonce = ?", (nonce,)
        ).fetchone()
        if row is None:
            return None
        return LinkChallenge(
            nonce=row["nonce"],
            session_id=row["session_id"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            redeemed_at=row["redeemed_at"],
        )

    @_locked
    def purge_expired_challenges(self, *, now: int) -> int:
        cur = self._conn.execute("DELETE FROM link_challenges WHERE expires_at < ?", (now,))
        self._conn.commit()
        return cur.rowcount

    # -- assertion anti-replay -------------------------------------------------

    @_locked
    def commit_redemption(
        self,
        *,
        nonce: str,
        org_uuid: str,
        assertion_expires_at: int,
        challenge: Optional[str],
        target_session: str,
        subject_kind: str,
        subject_id: str,
        dashboard_origin: str,
        now: int,
        session_expires_at: int,
    ) -> str:
        """Atomically redeem an assertion → identified session.

        The whole state transition — spend the single-use assertion nonce,
        redeem the single-use QR challenge (if any), and upgrade the target
        session to identified — commits as ONE transaction under the store
        lock. So under any level of concurrency the outcome is exactly one
        winner and every loser a clean, deterministic verdict, never a torn
        write or a SQLite API-misuse error. Returns:

        - ``"ok"``        — this call performed the redemption;
        - ``"replay"``    — the assertion nonce was already spent;
        - ``"challenge"`` — the QR challenge was gone / expired / already
          redeemed (a lost race), and NOTHING was committed (the nonce is
          rolled back, so a retry with a fresh challenge is still possible).

        Idempotency of the session upsert lets a QR flow upgrade an existing
        anonymous row in place while a same-browser flow inserts a fresh id.
        """
        try:
            spent = self._conn.execute(
                "INSERT OR IGNORE INTO consumed_assertions (nonce, org_uuid, expires_at)"
                " VALUES (?, ?, ?)",
                (nonce, org_uuid, assertion_expires_at),
            )
            if spent.rowcount != 1:
                self._conn.rollback()
                return "replay"

            if challenge is not None:
                redeemed = self._conn.execute(
                    "UPDATE link_challenges SET redeemed_at = ?"
                    " WHERE nonce = ? AND redeemed_at IS NULL AND expires_at >= ?",
                    (now, challenge, now),
                )
                if redeemed.rowcount != 1:
                    # Lost the single-use challenge race — undo the nonce
                    # spend too, so this whole attempt is a clean no-op.
                    self._conn.rollback()
                    return "challenge"

            self._conn.execute(
                "INSERT INTO link_sessions (session_id, org_uuid, subject_kind, subject_id,"
                " dashboard_origin, identified, created_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?, 1, ?, ?)"
                " ON CONFLICT (session_id) DO UPDATE SET"
                " org_uuid = excluded.org_uuid, subject_kind = excluded.subject_kind,"
                " subject_id = excluded.subject_id, dashboard_origin = excluded.dashboard_origin,"
                " identified = 1, expires_at = excluded.expires_at",
                (target_session, org_uuid, subject_kind, subject_id, dashboard_origin,
                 now, session_expires_at),
            )
            self._conn.commit()
            return "ok"
        except BaseException:
            self._conn.rollback()
            raise

    @_locked
    def assertion_consumed(self, nonce: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM consumed_assertions WHERE nonce = ?", (nonce,)
        ).fetchone()
        return row is not None

    @_locked
    def purge_expired_assertions(self, *, now: int) -> int:
        cur = self._conn.execute("DELETE FROM consumed_assertions WHERE expires_at < ?", (now,))
        self._conn.commit()
        return cur.rowcount

    # -- I12 view attribution --------------------------------------------------

    @_locked
    def record_view_attribution(
        self,
        session_id: str,
        token: str,
        *,
        subject_kind: str,
        subject_id: str,
        org_uuid: str,
        now: int,
    ) -> None:
        """Attribute an identified view to a subject — ONLY ever called for
        a grant that required auth (I12)."""
        self._conn.execute(
            "INSERT INTO link_view_attributions (session_id, token, subject_kind,"
            " subject_id, org_uuid, viewed_at) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (session_id, token) DO UPDATE SET viewed_at = excluded.viewed_at",
            (session_id, token, subject_kind, subject_id, org_uuid, now),
        )
        self._conn.commit()

    @_locked
    def view_attributions(self, token: Optional[str] = None) -> list:
        """All recorded view attributions (optionally for one token).

        The I12 assertion surface: for a no-auth grant this is empty even
        after an identified session fetched the envelope.
        """
        if token is None:
            rows = self._conn.execute(
                "SELECT * FROM link_view_attributions ORDER BY viewed_at"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM link_view_attributions WHERE token = ? ORDER BY viewed_at",
                (token,),
            ).fetchall()
        return [dict(r) for r in rows]
