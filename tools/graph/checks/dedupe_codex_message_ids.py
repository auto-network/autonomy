"""Audit + repair tool for auto-cpg1x: codex sources with duplicate
``message_id`` rows.

Background: W1's ``role='injected'`` change made previously-dropped codex
noise ``user_message`` entries start consuming turn-number slots. Any
codex session source ingested before that change, then grown after it,
re-parsed with its tail shifted to higher turn numbers — the incremental
cursor was purely positional (``turn_number > max_turn``), so it read the
shifted positions as new content and duplicated already-ingested turns.
Fixed going forward by message-id dedup (``_dedup_new_turns`` in
ingest.py); this tool finds and repairs sources already damaged before
that fix landed.

Usage (run host-side, against the real per-org DB tree)::

    python -m tools.graph.checks.dedupe_codex_message_ids --audit
    python -m tools.graph.checks.dedupe_codex_message_ids --audit --org autonomy
    python -m tools.graph.checks.dedupe_codex_message_ids --repair --org autonomy

``--audit`` (default) is strictly read-only and safe to run anytime.
``--repair`` deletes rows — always run ``--audit`` first and review the
report; ``--repair`` re-runs the same detection immediately before
deleting (no separate "trust an old report" step) so the actual delete
set is never stale relative to concurrent ingest activity.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from tools.graph.cross_org import list_org_slugs
from tools.graph.db import GraphDB, _orgs_dir


def find_duplicate_groups(db: GraphDB, source_id: str) -> list[list[dict]]:
    """Return groups of rows (thoughts + derivations) sharing a
    ``message_id`` within one source, for message_ids appearing more than
    once. Each row dict has ``tbl`` ('thought'|'derivation'), ``id``,
    ``message_id``, ``turn_number``."""
    rows = db.conn.execute(
        "SELECT 'thought' AS tbl, id, message_id, turn_number FROM thoughts"
        " WHERE source_id = ? AND message_id IS NOT NULL"
        " UNION ALL"
        " SELECT 'derivation' AS tbl, id, message_id, turn_number FROM derivations"
        " WHERE source_id = ? AND message_id IS NOT NULL",
        (source_id, source_id),
    ).fetchall()
    by_mid: dict[str, list[dict]] = {}
    for r in rows:
        by_mid.setdefault(r["message_id"], []).append(dict(r))
    return [group for group in by_mid.values() if len(group) > 1]


def audit_db(db_path: Path) -> list[dict]:
    """Read-only scan of one org DB. Returns a report row per affected
    codex source: {source_id, title, file_path, duplicate_message_ids,
    rows_to_delete}.

    Opens ``mode="ro"`` deliberately (auto-4y579): a normal read-write
    ``GraphDB`` open runs schema migrations on connection, including
    ``_migrate_message_id_unique`` — which auto-repairs any duplicate
    it finds before this function's own query ever runs. Read-only mode
    skips migrations entirely, so this reports the DB's true on-disk
    state rather than a state that was silently fixed as a side effect
    of opening it.
    """
    db = GraphDB(db_path, mode="ro")
    try:
        sources = db.conn.execute(
            "SELECT id, title, file_path FROM sources WHERE platform = 'codex-cli'"
        ).fetchall()
        report = []
        for src in sources:
            groups = find_duplicate_groups(db, src["id"])
            if not groups:
                continue
            rows_to_delete = sum(len(g) - 1 for g in groups)
            report.append({
                "source_id": src["id"],
                "title": src["title"],
                "file_path": src["file_path"],
                "duplicate_message_ids": len(groups),
                "rows_to_delete": rows_to_delete,
            })
        return report
    finally:
        db.close()


def repair_db(db_path: Path) -> list[dict]:
    """Delete duplicate rows in one org DB, keeping the lowest-turn_number
    row in each duplicate-message_id group. FTS triggers (thoughts_ad /
    derivations_ad, see schema.sql) clean the index automatically on
    DELETE. Returns the list of deleted rows: {source_id, table, id,
    turn_number, message_id}.

    Largely superseded by ``GraphDB``'s own ``_migrate_message_id_unique``
    migration (auto-4y579), which runs this exact dedupe-then-index logic
    automatically the moment ANY code opens a read-write connection to a
    violating DB — including the ``GraphDB(db_path)`` call this function
    itself makes below, so in practice this loop usually finds nothing
    left to do by the time it runs; the migration got there first. Kept
    as an explicit, on-demand tool for operators who want to force/verify
    a repair without depending on incidental connection-open timing.
    """
    db = GraphDB(db_path)
    deleted: list[dict] = []
    try:
        sources = db.conn.execute(
            "SELECT id FROM sources WHERE platform = 'codex-cli'"
        ).fetchall()
        for src in sources:
            groups = find_duplicate_groups(db, src["id"])
            for group in groups:
                group.sort(key=lambda r: r["turn_number"])
                for row in group[1:]:  # keep group[0] (lowest turn_number)
                    table = "thoughts" if row["tbl"] == "thought" else "derivations"
                    db.conn.execute(f"DELETE FROM {table} WHERE id = ?", (row["id"],))
                    deleted.append({
                        "source_id": src["id"], "table": table,
                        "id": row["id"], "turn_number": row["turn_number"],
                        "message_id": row["message_id"],
                    })
        db.conn.commit()
        return deleted
    finally:
        db.close()


def _org_db_paths(org: str | None) -> list[Path]:
    root = _orgs_dir()
    if org:
        path = root / f"{org}.db"
        return [path] if path.exists() else []
    return [root / f"{slug}.db" for slug in list_org_slugs(root=root)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", action="store_true", help="report only, no deletes (this is also the default with no flags)")
    parser.add_argument("--repair", action="store_true", help="delete duplicates (default: audit/report only)")
    parser.add_argument("--org", help="limit to one org slug (default: every org)")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = parser.parse_args(argv)

    db_paths = _org_db_paths(args.org)
    if not db_paths:
        print(f"no org DB(s) found for org={args.org!r}", file=sys.stderr)
        return 1

    if args.repair:
        all_deleted = []
        for db_path in db_paths:
            deleted = repair_db(db_path)
            if deleted:
                print(f"{db_path.stem}: deleted {len(deleted)} duplicate row(s) "
                      f"across {len({d['source_id'] for d in deleted})} source(s)")
            all_deleted.extend(deleted)
        if args.json:
            print(json.dumps(all_deleted, indent=2))
        return 0

    all_report = []
    for db_path in db_paths:
        report = audit_db(db_path)
        all_report.extend({**r, "org": db_path.stem} for r in report)

    if args.json:
        print(json.dumps(all_report, indent=2))
    else:
        if not all_report:
            print("OK: no intra-source duplicate message_ids found in any codex session source")
        for r in all_report:
            print(
                f"{r['org']}  {r['source_id'][:12]}  \"{r['title']}\"  "
                f"groups={r['duplicate_message_ids']} rows_to_delete={r['rows_to_delete']}  "
                f"({r['file_path']})"
            )
        total_rows = sum(r["rows_to_delete"] for r in all_report)
        if all_report:
            print(f"\n{len(all_report)} affected source(s), {total_rows} row(s) to delete total")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
