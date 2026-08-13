#!/usr/bin/env python3
"""Create and verify a standalone, WAL-correct registry SQLite snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inspect(path: Path) -> dict[str, Any]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchall()
        if integrity != [("ok",)]:
            raise RuntimeError(f"SQLite integrity check failed: {integrity!r}")
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        counts = {
            table: connection.execute(
                'SELECT COUNT(*) FROM "' + table.replace('"', '""') + '"'
            ).fetchone()[0]
            for table in tables
        }
        return {
            "sha256": _sha256(path),
            "bytes": path.stat().st_size,
            "page_count": connection.execute("PRAGMA page_count").fetchone()[0],
            "tables": counts,
        }
    finally:
        connection.close()


def snapshot(source: Path, destination: Path, metadata: Path) -> dict[str, Any]:
    source = source.resolve()
    destination = destination.resolve()
    metadata = metadata.resolve()
    if source == destination:
        raise ValueError("snapshot destination must differ from the live database")
    if not source.is_file():
        raise FileNotFoundError(f"live database does not exist: {source}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    metadata.parent.mkdir(parents=True, exist_ok=True)
    db_fd, db_tmp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    os.close(db_fd)
    db_tmp = Path(db_tmp_name)
    metadata_tmp: Path | None = None
    try:
        live = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        copy = sqlite3.connect(db_tmp)
        try:
            live.backup(copy)
        finally:
            copy.close()
            live.close()

        facts = _inspect(db_tmp)
        facts["format"] = "autonomy-registry-snapshot-v1"
        facts["source_name"] = source.name

        metadata_fd, metadata_tmp_name = tempfile.mkstemp(
            prefix=f".{metadata.name}.", dir=metadata.parent
        )
        metadata_tmp = Path(metadata_tmp_name)
        with os.fdopen(metadata_fd, "w", encoding="utf-8") as output:
            json.dump(facts, output, sort_keys=True, separators=(",", ":"))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())

        os.replace(db_tmp, destination)
        os.replace(metadata_tmp, metadata)
        return facts
    finally:
        db_tmp.unlink(missing_ok=True)
        if metadata_tmp is not None:
            metadata_tmp.unlink(missing_ok=True)


def verify(database: Path, metadata: Path | None = None) -> dict[str, Any]:
    database = database.resolve()
    if not database.is_file():
        raise FileNotFoundError(f"snapshot does not exist: {database}")
    facts = _inspect(database)
    if metadata is not None:
        expected = json.loads(metadata.read_text(encoding="utf-8"))
        for field in ("sha256", "bytes", "page_count", "tables"):
            if expected.get(field) != facts[field]:
                raise RuntimeError(
                    f"snapshot metadata mismatch for {field}: "
                    f"expected {expected.get(field)!r}, got {facts[field]!r}"
                )
    return facts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--source", type=Path, required=True)
    create.add_argument("--destination", type=Path, required=True)
    create.add_argument("--metadata", type=Path, required=True)
    check = commands.add_parser("verify")
    check.add_argument("--database", type=Path, required=True)
    check.add_argument("--metadata", type=Path)
    args = parser.parse_args()

    if args.command == "create":
        facts = snapshot(args.source, args.destination, args.metadata)
    else:
        facts = verify(args.database, args.metadata)
    print(json.dumps(facts, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
