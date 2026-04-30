"""One-time migration: tile + thread keyshape ``<coord>:<peer>`` → ``<peer>``.

Bead auto-1aef5 rekeys ``dashboard.coordinator-tile`` and
``dashboard.coordinator-thread`` from a composite ``<coord>:<peer>``
key to a peer-session-only key (peers self-publish; coordinator
curates via the publication-state machine). Existing rows already
written under the v1 keyshape are rewritten by this helper.

Idempotent: rows whose key has no ``:`` separator are left alone.
``dry_run=True`` reports what would change without committing.

The migration also bumps ``schema_revision`` from 1 to 2 on tile rows
so subsequent ``read_set`` calls resolve them at v2 without a per-call
upconvert. Thread payloads are shape-equivalent between v1 and v2 so
only the schema_revision changes there.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Iterable

from tools.graph.db import GraphDB
from tools.graph.schemas import upconvert_payload

from .schemas import (
    COORDINATOR_TILE_SET_ID,
    COORDINATOR_THREAD_SET_ID,
    SCHEMA_REVISION,
    TILE_SCHEMA_REVISION,
    THREAD_SCHEMA_REVISION,
)


@dataclass
class RekeyReport:
    """Per-set summary of the rekey pass."""
    set_id: str
    dry_run: bool
    rekeyed: int = 0
    already_bare: int = 0
    collisions: int = 0
    affected_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "set_id": self.set_id,
            "dry_run": self.dry_run,
            "rekeyed": self.rekeyed,
            "already_bare": self.already_bare,
            "collisions": self.collisions,
            "affected_ids": list(self.affected_ids),
        }


_REKEY_SET_TARGETS = (
    (COORDINATOR_TILE_SET_ID, TILE_SCHEMA_REVISION),
    (COORDINATOR_THREAD_SET_ID, THREAD_SCHEMA_REVISION),
)


def _rekey_one(
    db: GraphDB, set_id: str, target_revision: int, *, dry_run: bool,
) -> RekeyReport:
    report = RekeyReport(set_id=set_id, dry_run=dry_run)
    rows = db.conn.execute(
        "SELECT id, schema_revision, key, payload FROM settings "
        "WHERE set_id = ? AND excludes IS NULL",
        (set_id,),
    ).fetchall()

    # Pre-compute the new key per row + check for cross-row collisions
    # (two different rows that would both rekey to the same bare peer
    # session). Collisions get reported but not rewritten — operator
    # decides which to keep.
    bare_targets: dict[str, list[str]] = {}
    pending: list[tuple[str, int, str, str, dict]] = []
    for row in rows:
        old_key = row["key"]
        if ":" not in old_key:
            report.already_bare += 1
            continue
        new_key = old_key.split(":", 1)[1]
        if not new_key:
            # Defensive: a row keyed ``<coord>:`` with no peer half is
            # malformed; leave it alone rather than writing an empty key.
            report.already_bare += 1
            continue
        bare_targets.setdefault(new_key, []).append(row["id"])
        payload = json.loads(row["payload"])
        pending.append(
            (row["id"], int(row["schema_revision"]), old_key, new_key, payload),
        )

    collision_ids: set[str] = set()
    for new_key, ids in bare_targets.items():
        if len(ids) > 1:
            report.collisions += 1
            collision_ids.update(ids)

    for rid, stored_rev, old_key, new_key, payload in pending:
        if rid in collision_ids:
            continue

        # Upconvert the payload via the registered chain so the row
        # stored at the target_revision reflects the v2 shape (e.g.
        # tile.detail string → {context, choices}). Pass-through for
        # threads (no shape change).
        if stored_rev < target_revision:
            converted = upconvert_payload(
                set_id, stored_rev, target_revision, payload,
            )
            if converted is None:
                # Schema chain missing — leave the row alone rather than
                # silently dropping. Operator can register an upconverter
                # and re-run.
                continue
            new_payload = converted
        else:
            new_payload = payload

        report.affected_ids.append(rid)
        report.rekeyed += 1
        if not dry_run:
            db.conn.execute(
                "UPDATE settings SET key = ?, payload = ?, "
                "schema_revision = ? WHERE id = ?",
                (new_key, json.dumps(new_payload),
                 int(target_revision), rid),
            )
    if not dry_run:
        db.conn.commit()
    return report


def migrate_legacy_tile_thread_keys(
    db_path: str,
    *,
    dry_run: bool = False,
    set_targets: Iterable[tuple[str, int]] | None = None,
) -> list[RekeyReport]:
    """Rewrite ``<coord>:<peer>`` → ``<peer>`` keys for tile + thread sets.

    Operates on the SQLite Settings DB at *db_path* (typically
    ``data/personal.db`` or ``data/orgs/autonomy.db``). Returns one
    :class:`RekeyReport` per set processed.
    """
    targets = tuple(set_targets) if set_targets is not None else _REKEY_SET_TARGETS
    db = GraphDB(db_path)
    try:
        return [
            _rekey_one(db, set_id, target_revision, dry_run=dry_run)
            for set_id, target_revision in targets
        ]
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    """CLI: ``python -m tools.dashboard.plugins.coordinator_board.entrypoints.migrate``.

    --db PATH        SQLite Settings DB to migrate (required).
    --dry-run        Report changes without writing.
    """
    import argparse
    p = argparse.ArgumentParser(
        description="Rekey coordinator-tile + coordinator-thread "
                    "Setting rows from <coord>:<peer> to <peer>",
    )
    p.add_argument("--db", required=True, help="Path to SQLite Settings DB")
    p.add_argument("--dry-run", action="store_true",
                   help="Report what would change without writing")
    args = p.parse_args(argv)

    reports = migrate_legacy_tile_thread_keys(args.db, dry_run=args.dry_run)
    for r in reports:
        d = r.to_dict()
        print(
            f"{d['set_id']}: rekeyed={d['rekeyed']} "
            f"already_bare={d['already_bare']} "
            f"collisions={d['collisions']} "
            f"dry_run={d['dry_run']}"
        )
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())


_ = SCHEMA_REVISION  # silence unused import; kept for symmetry with schemas.py
