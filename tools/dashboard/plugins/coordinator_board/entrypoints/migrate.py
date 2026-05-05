"""One-time migrations for the coordinator-board Setting sets.

Two migrations live here:

* ``migrate_legacy_tile_thread_keys`` (bead auto-1aef5) — rewrites the
  composite ``<coord>:<peer>`` tile + thread keys to peer-session-only
  keys, bumping ``schema_revision`` from 1 to 2.

* ``drop_legacy_age_min`` (bead auto-fwwfu) — drops the now-legacy
  ``ageMin`` field from tile / thread / sprint payloads (the page
  derives "Nm ago" from ``member.updated_at`` instead) by upconverting
  rows from the now-prior revision (tile/thread #2, sprint #1) to the
  new revision (tile/thread #3, sprint #2).

Both helpers are idempotent. ``dry_run=True`` reports what would change
without committing.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Iterable

from tools.graph.db import GraphDB
from tools.graph.schemas import upconvert_payload

from .schemas import (
    COORDINATOR_SPRINT_SET_ID,
    COORDINATOR_THREAD_SET_ID,
    COORDINATOR_TILE_SET_ID,
    SCHEMA_REVISION,
    SPRINT_PRIOR_AGE_MIN_REVISION,
    SPRINT_SCHEMA_REVISION,
    THREAD_PRIOR_AGE_MIN_REVISION,
    THREAD_SCHEMA_REVISION,
    TILE_PRIOR_AGE_MIN_REVISION,
    TILE_SCHEMA_REVISION,
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
    (COORDINATOR_TILE_SET_ID, TILE_PRIOR_AGE_MIN_REVISION),
    (COORDINATOR_THREAD_SET_ID, THREAD_PRIOR_AGE_MIN_REVISION),
)


# (set_id, source_revision, target_revision) for the drop-ageMin pass.
# Source is the now-prior revision; target is the new ``ageMin``-free one.
_DROP_AGE_MIN_TARGETS = (
    (COORDINATOR_TILE_SET_ID,
     TILE_PRIOR_AGE_MIN_REVISION, TILE_SCHEMA_REVISION),
    (COORDINATOR_THREAD_SET_ID,
     THREAD_PRIOR_AGE_MIN_REVISION, THREAD_SCHEMA_REVISION),
    (COORDINATOR_SPRINT_SET_ID,
     SPRINT_PRIOR_AGE_MIN_REVISION, SPRINT_SCHEMA_REVISION),
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


@dataclass
class DropAgeMinReport:
    """Per-set summary of the drop-``ageMin`` pass."""
    set_id: str
    source_revision: int
    target_revision: int
    dry_run: bool
    rewritten: int = 0
    already_at_target: int = 0
    upconvert_failed: int = 0
    affected_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "set_id": self.set_id,
            "source_revision": self.source_revision,
            "target_revision": self.target_revision,
            "dry_run": self.dry_run,
            "rewritten": self.rewritten,
            "already_at_target": self.already_at_target,
            "upconvert_failed": self.upconvert_failed,
            "affected_ids": list(self.affected_ids),
        }


def _drop_age_min_one(
    db: GraphDB,
    set_id: str,
    source_revision: int,
    target_revision: int,
    *,
    dry_run: bool,
) -> DropAgeMinReport:
    report = DropAgeMinReport(
        set_id=set_id,
        source_revision=source_revision,
        target_revision=target_revision,
        dry_run=dry_run,
    )
    rows = db.conn.execute(
        "SELECT id, schema_revision, payload FROM settings "
        "WHERE set_id = ? AND excludes IS NULL",
        (set_id,),
    ).fetchall()

    for row in rows:
        stored_rev = int(row["schema_revision"])
        if stored_rev >= target_revision:
            report.already_at_target += 1
            continue
        if stored_rev != source_revision:
            # Out-of-band stored revision (e.g. tile rows still at v1 that
            # ``migrate_legacy_tile_thread_keys`` never visited). Leave
            # alone — operator can run the rekey migration first.
            continue
        payload = json.loads(row["payload"])
        converted = upconvert_payload(
            set_id, stored_rev, target_revision, payload,
        )
        if converted is None:
            report.upconvert_failed += 1
            continue
        report.affected_ids.append(row["id"])
        report.rewritten += 1
        if not dry_run:
            db.conn.execute(
                "UPDATE settings SET payload = ?, schema_revision = ? "
                "WHERE id = ?",
                (json.dumps(converted), int(target_revision), row["id"]),
            )
    if not dry_run:
        db.conn.commit()
    return report


def drop_legacy_age_min(
    db_path: str,
    *,
    dry_run: bool = False,
    set_targets: Iterable[tuple[str, int, int]] | None = None,
) -> list[DropAgeMinReport]:
    """Strip ``ageMin`` from tile / thread / sprint payloads.

    For each ``(set_id, source_revision, target_revision)`` in
    *set_targets*, loads every base member at the source revision,
    runs it through the registered upconvert chain (which strips
    ``ageMin``), and rewrites the row at the target revision.

    Idempotent: rows already at (or beyond) the target revision are
    counted under ``already_at_target`` and left alone.
    """
    targets = (
        tuple(set_targets) if set_targets is not None else _DROP_AGE_MIN_TARGETS
    )
    db = GraphDB(db_path)
    try:
        return [
            _drop_age_min_one(
                db, set_id, source_revision, target_revision,
                dry_run=dry_run,
            )
            for set_id, source_revision, target_revision in targets
        ]
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    """CLI: ``python -m tools.dashboard.plugins.coordinator_board.entrypoints.migrate``.

    --db PATH        SQLite Settings DB to migrate (required).
    --dry-run        Report what would change without writing.
    --rekey          Run the legacy ``<coord>:<peer>`` → ``<peer>`` rekey
                     pass (default if neither --rekey nor --drop-age-min
                     is given).
    --drop-age-min   Run the drop-``ageMin`` pass (tile/thread v2→v3,
                     sprint v1→v2).
    """
    import argparse
    p = argparse.ArgumentParser(
        description="Coordinator-board Settings migrations",
    )
    p.add_argument("--db", required=True, help="Path to SQLite Settings DB")
    p.add_argument("--dry-run", action="store_true",
                   help="Report what would change without writing")
    p.add_argument("--rekey", action="store_true",
                   help="Run the <coord>:<peer> → <peer> rekey migration")
    p.add_argument("--drop-age-min", action="store_true",
                   help="Run the drop-ageMin migration")
    args = p.parse_args(argv)

    rekey = args.rekey or not (args.rekey or args.drop_age_min)
    if rekey:
        reports = migrate_legacy_tile_thread_keys(args.db, dry_run=args.dry_run)
        for r in reports:
            d = r.to_dict()
            print(
                f"{d['set_id']}: rekeyed={d['rekeyed']} "
                f"already_bare={d['already_bare']} "
                f"collisions={d['collisions']} "
                f"dry_run={d['dry_run']}"
            )
    if args.drop_age_min:
        reports2 = drop_legacy_age_min(args.db, dry_run=args.dry_run)
        for r in reports2:
            d = r.to_dict()
            print(
                f"{d['set_id']} v{d['source_revision']}→v{d['target_revision']}: "
                f"rewritten={d['rewritten']} "
                f"already_at_target={d['already_at_target']} "
                f"upconvert_failed={d['upconvert_failed']} "
                f"dry_run={d['dry_run']}"
            )
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())


_ = SCHEMA_REVISION  # silence unused import; kept for symmetry with schemas.py
