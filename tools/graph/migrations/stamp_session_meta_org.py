"""One-shot, additive: stamp ``org`` into legacy-only .session_meta.json files (auto-5eu2s).

Ingest routes a session by its meta's ``org`` only; the legacy ``graph_org`` /
``graph_project`` fallbacks are gone. Run this on a node whose retained run
dirs predate 2026-08-30 (when every launcher began writing ``org``), or their
sessions skip at ingest ("no org in meta"). Home ran it 2026-09-28: 979 stamped.
Run as the node's app user (``docker exec -u autonomy``), never root.

A meta with no ``org`` but a legacy ``graph_org`` / ``graph_project`` gets
``org = graph_org or graph_project`` — the same precedence ingest's
session_target_org applied. The legacy keys stay in the file; nothing is removed.
Each rewrite is atomic (temp file in the same directory, then rename) and keeps
the file's mode.

    python3 -m tools.graph.migrations.stamp_session_meta_org <agent-runs dir>            # dry run
    python3 -m tools.graph.migrations.stamp_session_meta_org <agent-runs dir> --apply    # one pass

Prints: total metas, already-org, legacy-only (to stamp / stamped), skipped
(unreadable, not a dict, or no usable legacy value), and the remaining
legacy-only count after the pass.
"""

from __future__ import annotations

import glob
import json
import os
import sys
import tempfile


def _metas(root: str) -> list[str]:
    return sorted(set(
        glob.glob(os.path.join(root, "*", "sessions", ".session_meta.json"))
        + glob.glob(os.path.join(root, "*", ".session_meta.json"))
    ))


def _legacy_value(meta: dict) -> str | None:
    for key in ("graph_org", "graph_project"):
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _count(root: str) -> dict:
    c = {"total": 0, "has_org": 0, "legacy_only": 0, "neither": 0, "unreadable": 0}
    for path in _metas(root):
        try:
            with open(path, encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            c["unreadable"] += 1
            continue
        if not isinstance(meta, dict):
            c["unreadable"] += 1
            continue
        c["total"] += 1
        if meta.get("org"):
            c["has_org"] += 1
        elif _legacy_value(meta):
            c["legacy_only"] += 1
        else:
            c["neither"] += 1
    return c


def _write_atomic(path: str, meta: dict) -> None:
    mode = os.stat(path).st_mode & 0o7777
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".session_meta.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=2)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main(argv: list[str]) -> int:
    if not argv or argv[0].startswith("-"):
        print(__doc__)
        return 2
    root, apply = argv[0], "--apply" in argv[1:]
    if not os.path.isdir(root):
        print(f"not a directory: {root}", file=sys.stderr)
        return 2
    before = _count(root)
    print(f"root: {root}")
    print(f"before: {before}")
    stamped = skipped = 0
    by_org: dict[str, int] = {}
    for path in _metas(root):
        try:
            with open(path, encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            skipped += 1
            continue
        if not isinstance(meta, dict) or meta.get("org"):
            continue
        value = _legacy_value(meta)
        if value is None:
            skipped += 1
            continue
        by_org[value] = by_org.get(value, 0) + 1
        if apply:
            meta["org"] = value
            _write_atomic(path, meta)
        stamped += 1
    verb = "stamped" if apply else "would stamp"
    print(f"{verb}: {stamped}  skipped: {skipped}  by org: {dict(sorted(by_org.items()))}")
    if apply:
        print(f"after: {_count(root)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
