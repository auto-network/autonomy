"""One-shot backfill: re-tag compact-summary thoughts.

Prior to the compact_summary ingester support, Claude's context-compaction
continuation boilerplate ("This session is being continued from a previous
conversation that ran out of context.…") was ingested as role='user'. This
polluted FTS hits, attention queries, and human-input signals.

This script re-tags those rows to role='compact_summary'. The prefix is
verbatim Claude boilerplate, so false-positive risk is zero.

Usage:
    python3 -m tools.graph.migrations.backfill_compact_summary_role [--db PATH]

By default, uses $GRAPH_DB or ./data/graph.db.
"""

from __future__ import annotations
import argparse
import os
import sqlite3
import sys
from pathlib import Path


PREFIX = "This session is being continued from a previous conversation that ran out of context.%"


def resolve_db(arg: str | None) -> Path:
    if arg:
        return Path(arg)
    env_db = os.environ.get("GRAPH_DB")
    if env_db:
        return Path(env_db)
    cwd_db = Path.cwd() / "data" / "graph.db"
    if cwd_db.exists():
        return cwd_db
    raise SystemExit("graph.db not found (set GRAPH_DB or pass --db)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None, help="Path to graph.db")
    ap.add_argument("--dry-run", action="store_true", help="Count only, don't update")
    args = ap.parse_args()

    db_path = resolve_db(args.db)
    if not db_path.exists():
        print(f"ERROR: {db_path} does not exist", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path))

    before = conn.execute(
        "SELECT COUNT(*) FROM thoughts WHERE role = 'user' AND content LIKE ?",
        (PREFIX,),
    ).fetchone()[0]
    print(f"Candidate rows (role='user' + compact-summary prefix): {before}")

    if args.dry_run or before == 0:
        return 0

    cur = conn.execute(
        "UPDATE thoughts SET role = 'compact_summary' "
        "WHERE role = 'user' AND content LIKE ?",
        (PREFIX,),
    )
    conn.commit()
    changed = cur.rowcount
    print(f"Re-tagged rows: {changed}")

    total = conn.execute(
        "SELECT COUNT(*) FROM thoughts WHERE role = 'compact_summary'"
    ).fetchone()[0]
    print(f"Total role='compact_summary' rows after backfill: {total}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
