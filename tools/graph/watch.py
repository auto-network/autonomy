"""Live file watcher for Claude Code session ingestion.

Watches ~/.claude/projects/ for JSONL changes and incrementally ingests
new turns as they are written. Uses inotify on Linux for efficient
file change detection.
"""

from __future__ import annotations
import os
import sys
import time
from pathlib import Path

from .db import GraphDB, DEFAULT_DB
from .ingest import (
    _load_session_meta,
    _open_db_for_session,
    ingest_claude_code_session,
)


def watch_sessions(
    db_path: Path | None = None,
    projects_dir: Path | None = None,
    interval: float = 5.0,
    project_filter: str | None = None,
    verbose: bool = False,
):
    """Watch for JSONL file changes and incrementally ingest new turns.

    ``db_path=None`` (the default) routes each session to its own per-org
    DB based on ``.session_meta.json.graph_org`` (post-txg5.3). Passing
    an explicit ``db_path`` forces every session into that file — test /
    legacy behaviour.

    Uses polling with file size tracking. Checks every `interval` seconds
    for files that have grown since last check.
    """
    if projects_dir is None:
        projects_dir = Path.home() / ".claude" / "projects"

    if not projects_dir.exists():
        print(f"Error: {projects_dir} does not exist", file=sys.stderr)
        return

    pinned_db = GraphDB(db_path) if db_path is not None else None

    # Track file sizes to detect changes
    file_sizes: dict[str, int] = {}

    # Initial scan to populate sizes
    for jsonl_file in projects_dir.rglob("*.jsonl"):
        file_sizes[str(jsonl_file)] = jsonl_file.stat().st_size

    print(f"Watching {len(file_sizes)} session files in {projects_dir}")
    if project_filter:
        print(f"Filtering to projects matching: {project_filter}")
    print(f"Polling every {interval}s. Ctrl+C to stop.\n")

    try:
        while True:
            changed = []

            # Check for new or changed files
            for jsonl_file in projects_dir.rglob("*.jsonl"):
                path_str = str(jsonl_file)
                try:
                    current_size = jsonl_file.stat().st_size
                except OSError:
                    continue

                prev_size = file_sizes.get(path_str, 0)

                if current_size > prev_size:
                    # File has grown — pull project scope from .session_meta.json
                    meta = _load_session_meta(jsonl_file)
                    project = meta.get("graph_project")
                    if project_filter and project_filter not in (project or ""):
                        file_sizes[path_str] = current_size
                        continue

                    changed.append((jsonl_file, project, current_size - prev_size))
                    file_sizes[path_str] = current_size
                elif path_str not in file_sizes:
                    # New file
                    file_sizes[path_str] = current_size

            # Ingest changes
            for jsonl_file, project, delta_bytes in changed:
                session_db = pinned_db or _open_db_for_session(jsonl_file)
                try:
                    result = ingest_claude_code_session(
                        session_db, jsonl_file, project=project,
                    )
                    status = result["status"]
                    ts = time.strftime("%H:%M:%S")

                    if status == "updated":
                        print(f"  [{ts}] ~ {jsonl_file.stem[:12]}: "
                              f"+{result.get('new_thoughts', 0)} thoughts, "
                              f"+{result.get('new_derivations', 0)} derivations "
                              f"(+{delta_bytes:,} bytes)")
                    elif status == "ingested":
                        print(f"  [{ts}] + {jsonl_file.stem[:12]}: "
                              f"{result.get('thoughts', 0)} thoughts, "
                              f"{result.get('derivations', 0)} derivations "
                              f"— {result.get('title', '')[:40]}")
                    elif verbose:
                        print(f"  [{ts}] = {jsonl_file.stem[:12]}: {status}")

                except Exception as e:
                    print(f"  [ERROR] {jsonl_file.stem[:12]}: {e}", file=sys.stderr)
                finally:
                    if pinned_db is None:
                        session_db.close()

            time.sleep(interval)

    except KeyboardInterrupt:
        print("\nStopped watching.")
    finally:
        if pinned_db is not None:
            pinned_db.close()
