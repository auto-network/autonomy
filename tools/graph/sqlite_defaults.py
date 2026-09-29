"""Per-connection SQLite defaults every process applies (operator ruling
2026-09-29: never ``synchronous=FULL``).

``PRAGMA synchronous`` is a CONNECTION setting, not a database one (unlike
``journal_mode=WAL``, which is persisted in the file), so every opener
decides it and the library default is FULL: an fsync of the WAL on every
commit. On the fleet-sync apply path that is one durable commit per
replicated transaction — measured at 12 ms each on an overlay filesystem
(2026-09-29, auto-fkqz6), a ceiling of 83 transactions per second, 46
observed, the reason a catch-up crawls. With WAL, ``synchronous=NORMAL``
keeps the database consistent across application and OS crashes and syncs
the WAL before each checkpoint; only the last commits before a power loss
may roll back. Measured: 63,000 commits per second on the same disk.

Two places keep FULL deliberately and are left as written: the usage
accounting spool and ledger (tools/network/accounting), which write one
row per five-minute batch and state a durable-before-delivery contract.

:func:`apply` sets the defaults on one connection; the openers that set
WAL call it explicitly. :func:`install` wraps ``sqlite3.connect`` for the
whole process (through :mod:`tools.graph.sqlite_open_diag`), so every
other file-backed connection in that process gets them too; the dashboard
worker, the serving connector and the graph CLI install it at start.
"""

from __future__ import annotations

import sqlite3

#: WAL-appropriate durability: consistent across crashes, no fsync per commit.
SYNCHRONOUS = "NORMAL"


def apply(conn: sqlite3.Connection) -> sqlite3.Connection:
    """Set this process's per-connection defaults on *conn*; returns it."""
    try:
        conn.execute(f"PRAGMA synchronous={SYNCHRONOUS}")
    except sqlite3.Error:
        # A connection mid-transaction, or already closed: the caller's
        # own pragma (if any) stands; this must never fail an open.
        pass
    return conn


def install() -> None:
    """Apply the defaults to every file-backed connection this process
    opens from now on. Idempotent."""
    from tools.graph import sqlite_open_diag

    sqlite_open_diag.install()
