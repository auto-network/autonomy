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

All timestamps are unix seconds, passed in by the caller — the store
never reads the wall clock, which is what makes TTL behavior testable.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Optional

from tools.network.idkit import RevocationRecord, RevocationSet

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


class RegistryStore:
    def __init__(self, db_path: str = ":memory:"):
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- orgs ---------------------------------------------------------------

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
            # revocations died with its binding.
            self._conn.execute("DELETE FROM orgs WHERE org_uuid = ?", (org_uuid,))
            self._conn.execute("DELETE FROM links WHERE org_uuid = ?", (org_uuid,))
            self._conn.execute("DELETE FROM revocations WHERE org_uuid = ?", (org_uuid,))
            self._conn.execute("DELETE FROM topic_hints WHERE org_uuid = ?", (org_uuid,))
            self._conn.execute("DELETE FROM topic_bundles WHERE org_uuid = ?", (org_uuid,))
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

    def renew_org(self, org_uuid: str, *, now: int, expires_at: int) -> None:
        self._conn.execute(
            "UPDATE orgs SET expires_at = ?, renewed_at = ? WHERE org_uuid = ?",
            (expires_at, now, org_uuid),
        )
        self._conn.commit()

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

    def update_recovery_policy(
        self, org_uuid: str, recovery_policy: str, recovery_pub: Optional[str]
    ) -> None:
        self._conn.execute(
            "UPDATE orgs SET recovery_policy = ?, recovery_pub = ? WHERE org_uuid = ?",
            (recovery_policy, recovery_pub, org_uuid),
        )
        self._conn.commit()

    def rebind_history(self, org_uuid: str) -> list:
        rows = self._conn.execute(
            "SELECT old_root_pub, new_root_pub, rebound_at FROM rebinds"
            " WHERE org_uuid = ? ORDER BY rebound_at",
            (org_uuid,),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- links --------------------------------------------------------------

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

    def revoke_link(self, token: str, *, now: int) -> None:
        self._conn.execute("UPDATE links SET revoked_at = ? WHERE token = ?", (now, token))
        self._conn.commit()

    # -- revocations ---------------------------------------------------------

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

    def revocation_set(self, org_uuid: str) -> RevocationSet:
        """The org's live denylist, as the object ``verify_chain`` consumes."""
        rows = self._conn.execute(
            "SELECT record FROM revocations WHERE org_uuid = ?", (org_uuid,)
        ).fetchall()
        rset = RevocationSet()
        for row in rows:
            rset.add(RevocationRecord.from_json(row["record"]))
        return rset

    def get_revocation(self, org_uuid: str, revoked_key_id: str) -> Optional[RevocationRecord]:
        row = self._conn.execute(
            "SELECT record FROM revocations WHERE org_uuid = ? AND revoked_key_id = ?",
            (org_uuid, revoked_key_id),
        ).fetchone()
        return RevocationRecord.from_json(row["record"]) if row else None

    def purge_expired_revocations(self, *, now: int) -> int:
        """I7 sweep: drop records past the revoked key's natural expiry."""
        cur = self._conn.execute("DELETE FROM revocations WHERE expires_at < ?", (now,))
        self._conn.commit()
        return cur.rowcount

    # -- topics (F3 broker path — hints + encrypted mailbox, L6) ---------------

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

    def latest_hint(self, org_uuid: str, topic: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT seq, heads, published_at FROM topic_hints"
            " WHERE org_uuid = ? AND topic = ? ORDER BY seq DESC LIMIT 1",
            (org_uuid, topic),
        ).fetchone()
        if row is None:
            return None
        return {"seq": row["seq"], "heads": json.loads(row["heads"]), "published_at": row["published_at"]}

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
