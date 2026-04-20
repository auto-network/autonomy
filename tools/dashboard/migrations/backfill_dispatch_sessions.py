"""Back-fill tmux_sessions rows for historical dispatch runs.

Scans ``data/agent-runs/*/sessions/`` for existing agent runs and INSERTs a
``type='dispatch'``, ``is_live=0`` row per historical run that does not yet
have one. Idempotent — reruns are safe.

Usage::

    from tools.dashboard.migrations import backfill_dispatch_sessions
    backfill_dispatch_sessions.backfill(Path("data/agent-runs"))

Invoked manually or from dashboard boot. Not invoked automatically from
session_monitor.start().
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def _find_jsonl(sessions_dir: Path) -> Path | None:
    """Return the first non-subagent JSONL under sessions/, or None."""
    if not sessions_dir.exists():
        return None
    for jsonl in sorted(sessions_dir.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime):
        if "subagents" in jsonl.parts:
            continue
        return jsonl
    return None


def _bead_id_from_run_name(run_name: str) -> str | None:
    """Extract bead id from ``<bead>-MMDD-HHMMSS`` style run directory names."""
    parts = run_name.rsplit("-", 2)
    if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
        return parts[0]
    return None


def backfill(agent_runs_dir: Path, *, db_path: Path | None = None) -> int:
    """Insert one ``type='dispatch'``, ``is_live=0`` row per historical run.

    Returns the number of rows inserted. Idempotent: running twice is safe.
    """
    from tools.dashboard.dao import dashboard_db as db_mod

    if db_path is not None:
        db_mod.init_db(db_path)
    conn = db_mod.get_conn()

    inserted = 0
    if not agent_runs_dir.exists():
        return inserted

    for run_dir in sorted(agent_runs_dir.iterdir()):
        if not run_dir.is_dir():
            continue
        run_name = run_dir.name
        existing = conn.execute(
            "SELECT 1 FROM tmux_sessions WHERE tmux_name=?", (run_name,)
        ).fetchone()
        if existing:
            continue

        sessions_dir = run_dir / "sessions"
        jsonl = _find_jsonl(sessions_dir)
        if jsonl is None:
            continue

        session_uuid = jsonl.stem
        project = jsonl.parent.name
        bead_id = _bead_id_from_run_name(run_name)
        mtime = jsonl.stat().st_mtime
        try:
            conn.execute(
                "INSERT INTO tmux_sessions"
                " (tmux_name, type, project, bead_id, jsonl_path, session_uuid,"
                "  resolution_dir, session_uuids, curr_jsonl_file,"
                "  created_at, is_live, last_activity)"
                " VALUES (?, 'dispatch', ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
                (
                    run_name, project, bead_id, str(jsonl), session_uuid,
                    str(jsonl.parent), json.dumps([session_uuid]),
                    str(jsonl), mtime, mtime,
                ),
            )
            inserted += 1
        except sqlite3.IntegrityError:
            continue
    conn.commit()
    if inserted:
        logger.info("backfill_dispatch_sessions: inserted %d rows", inserted)
    return inserted


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--agent-runs", required=True, type=Path)
    ap.add_argument("--db", type=Path, default=None)
    args = ap.parse_args()
    inserted = backfill(args.agent_runs, db_path=args.db)
    print(f"inserted={inserted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
