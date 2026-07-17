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

All timestamps are unix seconds, passed in by the caller — the store
never reads the wall clock, which is what makes TTL behavior testable.
"""

from __future__ import annotations

import functools
import json
import sqlite3
import threading
from dataclasses import dataclass
from typing import Optional

from tools.network.idkit import RevocationRecord, RevocationSet

from .witness import build_entry, entry_id as _entry_id

_SCHEMA = """
CREATE TABLE IF NOT EXISTS orgs (
    org_uuid        TEXT PRIMARY KEY,
    root_pub        TEXT NOT NULL,
    recovery_policy TEXT NOT NULL,
    recovery_pub    TEXT,
    created_at      INTEGER NOT NULL,
    expires_at      INTEGER NOT NULL,
    renewed_at      INTEGER,
    endpoint_hints  TEXT
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
    meta         TEXT NOT NULL,
    created_at   INTEGER NOT NULL,
    expires_at   INTEGER,
    revoked_at   INTEGER,
    signer_pub   TEXT NOT NULL,
    subject_kind TEXT NOT NULL,
    subject_id   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_links_org ON links (org_uuid);

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

CREATE TABLE IF NOT EXISTS attestations (
    attestation_id TEXT PRIMARY KEY,
    attestor_pub   TEXT NOT NULL,
    subject_pub    TEXT NOT NULL,
    claim_type     TEXT NOT NULL,
    claim_value    TEXT NOT NULL,
    record         TEXT NOT NULL,
    ts             INTEGER NOT NULL,
    expires_at     INTEGER NOT NULL,
    received_at    INTEGER NOT NULL
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
        self._conn.commit()

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
        self._conn.execute(
            "INSERT INTO orgs (org_uuid, root_pub, recovery_policy, recovery_pub,"
            " created_at, expires_at, renewed_at, endpoint_hints)"
            " VALUES (?, ?, ?, ?, ?, ?, NULL, ?)",
            (
                org_uuid,
                root_pub,
                recovery_policy,
                recovery_pub,
                now,
                expires_at,
                json.dumps(endpoint_hints) if endpoint_hints is not None else None,
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
        self, org_uuid: str, recovery_policy: str, recovery_pub: Optional[str]
    ) -> None:
        self._conn.execute(
            "UPDATE orgs SET recovery_policy = ?, recovery_pub = ? WHERE org_uuid = ?",
            (recovery_policy, recovery_pub, org_uuid),
        )
        self._conn.commit()

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
        )

    @_locked
    def create_link(self, grant: LinkGrant) -> None:
        self._conn.execute(
            "INSERT INTO links (token, org_uuid, target_uuid, target_type, meta,"
            " created_at, expires_at, revoked_at, signer_pub, subject_kind, subject_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)",
            (
                grant.token,
                grant.org_uuid,
                grant.target_uuid,
                grant.target_type,
                json.dumps(grant.meta),
                grant.created_at,
                grant.expires_at,
                grant.signer_pub,
                grant.subject_kind,
                grant.subject_id,
            ),
        )
        self._conn.commit()

    @_locked
    def revoke_link(self, token: str, *, now: int) -> None:
        self._conn.execute("UPDATE links SET revoked_at = ? WHERE token = ?", (now, token))
        self._conn.commit()

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
        rows = self._conn.execute(
            "SELECT * FROM listings WHERE publisher = ? AND name = ? ORDER BY seq",
            (publisher, name),
        ).fetchall()
        return [self._listing_row(r) for r in rows]

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
        edges = self._conn.execute(
            "SELECT old_root_pub, new_root_pub FROM rebinds WHERE org_uuid = ?",
            (org_uuid,),
        ).fetchall()
        continuity = {current_root}
        changed = True
        while changed:
            changed = False
            for edge in edges:
                if edge["new_root_pub"] in continuity and edge["old_root_pub"] not in continuity:
                    continuity.add(edge["old_root_pub"])
                    changed = True
        return continuity

    # -- attestations ------------------------------------------------------------

    def add_attestation(
        self, attestation_id: str, payload: dict, record: str, *, now: int
    ) -> None:
        """Store a signature-verified attestation record.

        Content-addressed: redelivering the identical record is a no-op
        (``INSERT OR IGNORE`` on the id, which commits to every byte).
        """
        self._conn.execute(
            "INSERT OR IGNORE INTO attestations (attestation_id, attestor_pub,"
            " subject_pub, claim_type, claim_value, record, ts, expires_at,"
            " received_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (attestation_id, payload["attestor"], payload["subject"],
             payload["claim_type"], payload["claim_value"], record,
             payload["ts"], payload["ts"] + payload["ttl"], now),
        )
        self._conn.commit()

    def attestations_for_subject(self, subject_pub: str, *, now: int) -> list:
        """Live (unexpired) attestation records about *subject_pub*."""
        rows = self._conn.execute(
            "SELECT * FROM attestations WHERE subject_pub = ? AND expires_at > ?"
            " ORDER BY ts, attestation_id",
            (subject_pub, now),
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
