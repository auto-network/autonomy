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

This module lives in the network tree because the registry's shipped tree
(the relay) opens SQLite stores too and never carries ``tools.graph``: a
``tools.graph`` import in the ledger store or the registry store blocked
the registry deploy at its import-closure preflight (2026-09-30 02:26Z).
:func:`apply` sets the defaults on one connection. The process-wide
install (wrapping ``sqlite3.connect``) is the graph package's
(``tools.graph.sqlite_defaults.install``), which the dashboard worker, the
serving connector and the graph CLI call at start.
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
