"""Authoring harness and origin-of-authority probe for personal GraphDB sync.

Bulk database snapshots are retired: a joining machine bootstraps by sweeping
the serving store's keyspace beneath a frontier ``F`` (SWEEP gives all keys
<= F, PULL gives all keys > F), never by installing a peer's database file.
What survives here is the authored-write harness the tests drive and the
node-local ledger probe the scheduler consults.
"""

from __future__ import annotations

from pathlib import Path
import logging
import sqlite3

from tools.graph.db import GraphDB

from .catalog import MutationCatalog
from .streaming import ensure_streaming_indexes


ALPHA_VERSION = "1.0-alpha.1"

_log = logging.getLogger(__name__)


class AlphaError(RuntimeError):
    pass


class FleetSyncAlpha:
    def __init__(self, path: Path, origin_incarnation: str) -> None:
        self.path = path
        # Alpha owns an explicit caller-supplied authored context. Production
        # GraphDB opens auto-attach the connection-bound writer hook instead.
        self.graph = GraphDB(path, attach_fleet_sync=False)
        self.catalog = MutationCatalog(self.graph.conn, origin_incarnation)
        self.catalog.install()
        ensure_streaming_indexes(self.graph.conn)

    def close(self) -> None:
        self.graph.close()

    def __enter__(self) -> "FleetSyncAlpha":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def author(self, timestamp_ns: int, transaction_id: str):
        return self.catalog.transaction(timestamp_ns, transaction_id)


def founded_ledger_rows(path: Path) -> int:
    """Rows in this store's own ledger (events + heads), 0 when none/absent.

    The policy keeps the ledger node-local, so it is never replicated. Its
    presence means this store is an ORIGIN of authority, not a joiner."""
    if not Path(path).exists():
        return 0
    try:
        conn = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True)
    except sqlite3.Error:
        return 0
    try:
        tables = {
            str(row[0]) for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        total = 0
        for table in ("ledger_events", "ledger_heads"):
            if table in tables:
                total += int(
                    conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                )
        return total
    except sqlite3.Error:
        return 0
    finally:
        conn.close()
