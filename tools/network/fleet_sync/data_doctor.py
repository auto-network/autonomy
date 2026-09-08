"""Scan a personal/org GraphDB for data that will break a fleet-sync
checkpoint install, in one pass, instead of discovering each class one
failed install at a time.

Two independent invariant classes the checkpoint installer depends on:

1. Foreign-key orphans in NOT NULL FK tables -- a row whose required
   parent is absent. These are permanently unrepresentable on a fresh
   receiver and get skipped+quarantined during install (see sync.py);
   they are real, safe-to-delete corruption (see graph note on the
   2026-04-20/21 per-org DB migration, which already treated this
   exact shape as droppable debris and just didn't fully apply it here).

2. ``settings`` base-role logical addresses (set_id, schema_revision,
   key, supersedes IS NULL, excludes IS NULL) that don't have EXACTLY
   ONE ``deprecated=0`` row. Zero is fine (fully retired, nothing
   live). More than one is a genuine invariant violation -- normal
   version history is supposed to converge on a single current row --
   and needs a judgment call (usually: keep the most recent, deprecate
   the rest), not a blind delete.

Usage:
    python3 -m tools.network.fleet_sync.data_doctor <db_path> [--json]
    python3 -m tools.network.fleet_sync.data_doctor <db_path> --delete-orphans [--yes]
"""

from __future__ import annotations

import argparse
import json
import sys

from tools.graph.db import GraphDB

#: (table, fk_column, parent_table) for every NOT NULL foreign key in
#: schema.sql. A row here whose fk_column value has no matching parent
#: row is unrepresentable and safe to delete -- the same class sync.py
#: already skips+quarantines on the receiving side.
NOT_NULL_FKS = (
    ("thoughts", "source_id", "sources"),
    ("derivations", "source_id", "sources"),
    ("node_refs", "node_id", "nodes"),
    ("note_comments", "source_id", "sources"),
    ("note_versions", "source_id", "sources"),
)

#: (table, fk_column, parent_table) for nullable FKs (ON DELETE SET
#: NULL / plain nullable). A dangling value here is not fatal to a
#: checkpoint install, but it is still a real inconsistency worth
#: reporting -- report only, no delete mode, since NULLing the column
#: is the correct repair, not removing the row.
NULLABLE_FKS = (
    ("derivations", "thought_id", "thoughts"),
    ("claims", "source_id", "sources"),
    ("nodes", "parent_id", "nodes"),
    ("captures", "thread_id", "threads"),
    ("captures", "source_id", "sources"),
)


def scan_fk_orphans(conn, fks):
    results = []
    for table, column, parent in fks:
        row = conn.execute(
            f'SELECT COUNT(*) FROM "{table}" t '
            f'LEFT JOIN "{parent}" p ON p.id = t."{column}" '
            f'WHERE t."{column}" IS NOT NULL AND p.id IS NULL'
        ).fetchone()
        if row[0]:
            results.append({"table": table, "column": column,
                             "parent": parent, "orphans": row[0]})
    return results


def scan_settings_live_rows(conn):
    # publication_state is part of the logical address (see catalog.py's
    # _live_row: set_id, schema_revision, key, publication_state together
    # key a base row) -- grouping without it conflates e.g. a "raw" and a
    # "canonical" row of the same set_id/key into one group and produces a
    # false positive/negative on which one is "the" live row.
    rows = conn.execute(
        'SELECT set_id, schema_revision, "key", publication_state, COUNT(*) AS n, '
        'SUM(CASE WHEN deprecated=0 THEN 1 ELSE 0 END) AS live_n '
        'FROM settings WHERE supersedes IS NULL AND excludes IS NULL '
        'GROUP BY set_id, schema_revision, "key", publication_state '
        'HAVING live_n > 1'
    ).fetchall()
    return [
        {"set_id": r[0], "schema_revision": r[1], "key": r[2],
         "publication_state": r[3], "physical_rows": r[4], "live_rows": r[5]}
        for r in rows
    ]


def delete_orphans(conn, fks, *, dry_run: bool) -> int:
    total = 0
    for table, column, parent in fks:
        cur = conn.execute(
            f'SELECT t.id FROM "{table}" t '
            f'LEFT JOIN "{parent}" p ON p.id = t."{column}" '
            f'WHERE t."{column}" IS NOT NULL AND p.id IS NULL'
        )
        ids = [r[0] for r in cur.fetchall()]
        if not ids and not dry_run:
            continue
        total += len(ids)
        if dry_run or not ids:
            continue
        conn.executemany(f'DELETE FROM "{table}" WHERE id=?', [(i,) for i in ids])
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db_path")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--delete-orphans", action="store_true",
                         help="delete NOT-NULL-FK orphans (report-only otherwise)")
    parser.add_argument("--yes", action="store_true",
                         help="skip the confirmation prompt for --delete-orphans")
    args = parser.parse_args()

    g = GraphDB(args.db_path)
    try:
        fk_orphans = scan_fk_orphans(g.conn, NOT_NULL_FKS)
        nullable_dangling = scan_fk_orphans(g.conn, NULLABLE_FKS)
        settings_anomalies = scan_settings_live_rows(g.conn)

        if args.delete_orphans:
            if not args.yes:
                total = sum(r["orphans"] for r in fk_orphans)
                print(f"Would delete {total} orphaned row(s) across "
                      f"{len(fk_orphans)} table(s). Re-run with --yes to apply.",
                      file=sys.stderr)
                for r in fk_orphans:
                    print(f"  {r['table']}.{r['column']} -> {r['parent']}: "
                          f"{r['orphans']} orphan(s)", file=sys.stderr)
                return 1
            deleted = delete_orphans(g.conn, NOT_NULL_FKS, dry_run=False)
            g.conn.commit()
            print(f"Deleted {deleted} orphaned row(s).")
            return 0

        report = {
            "fk_orphans": fk_orphans,
            "nullable_dangling_fks": nullable_dangling,
            "settings_multi_live": settings_anomalies,
            "clean": not (fk_orphans or nullable_dangling or settings_anomalies),
        }
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            if report["clean"]:
                print("Clean -- no known sync-breaking data issues found.")
            else:
                if fk_orphans:
                    print("Foreign-key orphans (NOT NULL, unrepresentable, "
                          "safe to delete -- run with --delete-orphans):")
                    for r in fk_orphans:
                        print(f"  {r['table']}.{r['column']} -> {r['parent']}: "
                              f"{r['orphans']} orphan(s)")
                if nullable_dangling:
                    print("Dangling nullable FK references (repair = NULL the "
                          "column, not a delete; report only):")
                    for r in nullable_dangling:
                        print(f"  {r['table']}.{r['column']} -> {r['parent']}: "
                              f"{r['orphans']} dangling value(s)")
                if settings_anomalies:
                    print("Settings groups with more than one live row "
                          "(genuine invariant violation, needs a judgment call "
                          "-- which row is actually current):")
                    for r in settings_anomalies:
                        print(f"  {r['set_id']} / {r['key']} (rev {r['schema_revision']}, "
                              f"pub={r['publication_state']}): "
                              f"{r['live_rows']} live of {r['physical_rows']} physical rows")
        return 0 if report["clean"] else 2
    finally:
        g.close()


if __name__ == "__main__":
    raise SystemExit(main())
