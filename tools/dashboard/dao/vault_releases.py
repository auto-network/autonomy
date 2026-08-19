"""The durable record of secret releases (auto-pw9bs.5).

Every ``delivered``-mode secret release is recorded here BEFORE the delivery
layer materialises anything, so the only crash state is a record with no
file, never a file with no record (design ``graph://0c206bd8-1c6`` §4.2).
The record is a locator and a deadline — it never holds the plaintext, the
sealed key, or the ciphertext body: those live in the session's ramfs
subdirectory and nowhere else. A thief holding this database learns which
secret went to which session and when it was destroyed, not what it was.

The sweeper (:mod:`tools.dashboard.vault_release_sweeper`) reads this store
to destroy releases at their deadline and to reconcile across a dashboard
restart. Destruction is recorded in place (``shredded_at`` /
``shred_reason``) rather than by deleting the row, so the record of a
release outlives the secret and a post-hoc audit can see that every
release was destroyed and why.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from tools.data_paths import resolve_store

SCHEMA_VERSION = 1
SCHEMA = """
CREATE TABLE IF NOT EXISTS vault_releases (
    id             TEXT PRIMARY KEY,
    session        TEXT NOT NULL,
    setting_name   TEXT NOT NULL,
    release_mode   TEXT NOT NULL,
    delivered_at   INTEGER NOT NULL,
    expires_at     INTEGER NOT NULL,
    container_path TEXT NOT NULL,
    host_path      TEXT NOT NULL,
    shredded_at    INTEGER,
    shred_reason   TEXT
);
CREATE INDEX IF NOT EXISTS idx_vault_releases_outstanding
    ON vault_releases(expires_at) WHERE shredded_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_vault_releases_session
    ON vault_releases(session) WHERE shredded_at IS NULL;
"""

#: Valid values of ``shred_reason`` — a closed set so the audit vocabulary
#: cannot drift. ``expired`` = the deadline passed; ``session_end`` = the
#: session's whole subdirectory was reclaimed; ``reconciled`` = destroyed by
#: start-up reconciliation after a restart; ``orphaned`` = the session no
#: longer exists.
SHRED_REASONS = frozenset({"expired", "session_end", "reconciled", "orphaned"})


class VaultReleaseStoreError(RuntimeError):
    """The release record store could not be read or written."""


def db_path() -> Path:
    return resolve_store("vault_releases")


def init_db(path: Path | str | None = None) -> None:
    target = Path(path) if path is not None else db_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(target, timeout=5) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                conn.executescript(SCHEMA)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif version != SCHEMA_VERSION:
                raise VaultReleaseStoreError(
                    f"unsupported vault-release schema v{version}; "
                    f"expected v{SCHEMA_VERSION}"
                )
    except VaultReleaseStoreError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise VaultReleaseStoreError(
            f"could not initialize vault-release store: {exc}"
        ) from exc


def _connect(path: Path | str | None = None) -> sqlite3.Connection:
    target = Path(path) if path is not None else db_path()
    init_db(target)
    conn = sqlite3.connect(target, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def record_release(
    *,
    id: str,
    session: str,
    setting_name: str,
    release_mode: str,
    expires_at: int,
    container_path: str,
    host_path: str,
    delivered_at: int | None = None,
    path: Path | str | None = None,
    now: int | None = None,
) -> dict:
    """Commit a release record. THIS RUNS BEFORE THE DELIVERY LAYER RETURNS.

    The ordering is the invariant the whole store exists for: the caller
    must commit the record here and only then materialise the sealed
    response, so a crash between the two leaves a record with no file — a
    state the sweeper cleans — and never a file with no record, which
    nothing would ever find. A caller that materialises first and records
    second has silently defeated the store; the delivery bead's contract
    is record-then-deliver, asserted there.

    ``host_path`` is where the SWEEPER unlinks — the file's location on the
    host, inside the per-session ramfs subdirectory
    (``agents.secret_ramfs.DELIVERY_MOUNT/<session>/...``). ``container_path``
    is the same file as the session sees it, carried for the audit only.
    Neither is the secret; both are paths.
    """
    if release_mode != "delivered":
        # Only 'delivered' mode materialises a file that needs sweeping;
        # 'mediated' and 'client_operation' leave no host artifact, so a
        # record here would describe a file that never exists. Refuse
        # rather than record a shred target the sweeper can never satisfy.
        raise ValueError(
            f"only 'delivered' releases are recorded here, not {release_mode!r}"
        )
    for name, value in (
        ("id", id), ("session", session), ("setting_name", setting_name),
        ("container_path", container_path), ("host_path", host_path),
    ):
        if not isinstance(value, str) or not value:
            raise ValueError(f"{name} must be a non-empty string")
    if not isinstance(expires_at, int) or isinstance(expires_at, bool):
        raise ValueError("expires_at must be an int (unix ms)")
    stamp = int(time.time() * 1000) if now is None else int(now)
    delivered = stamp if delivered_at is None else int(delivered_at)
    try:
        with _connect(path) as conn:
            conn.execute(
                """
                INSERT INTO vault_releases(
                    id, session, setting_name, release_mode, delivered_at,
                    expires_at, container_path, host_path, shredded_at,
                    shred_reason
                ) VALUES(?,?,?,?,?,?,?,?,NULL,NULL)
                """,
                (
                    id, session, setting_name, release_mode, delivered,
                    int(expires_at), container_path, host_path,
                ),
            )
    except sqlite3.IntegrityError as exc:
        raise VaultReleaseStoreError(
            f"release id {id!r} already recorded: {exc}"
        ) from exc
    except sqlite3.Error as exc:
        raise VaultReleaseStoreError(
            f"could not record release: {exc}"
        ) from exc
    return get(id, path=path)


def get(release_id: str, *, path: Path | str | None = None) -> dict | None:
    try:
        with _connect(path) as conn:
            row = conn.execute(
                "SELECT * FROM vault_releases WHERE id = ?", (release_id,),
            ).fetchone()
    except sqlite3.Error as exc:
        raise VaultReleaseStoreError(f"could not read release: {exc}") from exc
    return dict(row) if row is not None else None


def outstanding(*, path: Path | str | None = None) -> list[dict]:
    """Every release not yet shredded, oldest deadline first — the sweeper's
    and reconciliation's work-list."""
    try:
        with _connect(path) as conn:
            rows = conn.execute(
                "SELECT * FROM vault_releases WHERE shredded_at IS NULL "
                "ORDER BY expires_at, id"
            ).fetchall()
    except sqlite3.Error as exc:
        raise VaultReleaseStoreError(
            f"could not list outstanding releases: {exc}"
        ) from exc
    return [dict(row) for row in rows]


def outstanding_sessions(*, path: Path | str | None = None) -> set[str]:
    """The distinct sessions with any outstanding release — used to decide
    which per-session ramfs subdirectories are still live."""
    try:
        with _connect(path) as conn:
            rows = conn.execute(
                "SELECT DISTINCT session FROM vault_releases "
                "WHERE shredded_at IS NULL"
            ).fetchall()
    except sqlite3.Error as exc:
        raise VaultReleaseStoreError(
            f"could not list outstanding sessions: {exc}"
        ) from exc
    return {row["session"] for row in rows}


def mark_shredded(
    release_id: str,
    *,
    reason: str,
    path: Path | str | None = None,
    now: int | None = None,
) -> bool:
    """Record that a release's file was destroyed. Idempotent: a second
    call on an already-shredded row keeps the FIRST reason and timestamp,
    because the first destruction is the true one and a re-sweep must not
    rewrite history. Returns True iff this call performed the transition."""
    if reason not in SHRED_REASONS:
        raise ValueError(
            f"shred reason {reason!r} not in {sorted(SHRED_REASONS)}"
        )
    stamp = int(time.time() * 1000) if now is None else int(now)
    try:
        with _connect(path) as conn:
            cur = conn.execute(
                "UPDATE vault_releases SET shredded_at = ?, shred_reason = ? "
                "WHERE id = ? AND shredded_at IS NULL",
                (stamp, reason, release_id),
            )
            return cur.rowcount > 0
    except sqlite3.Error as exc:
        raise VaultReleaseStoreError(
            f"could not mark release shredded: {exc}"
        ) from exc
