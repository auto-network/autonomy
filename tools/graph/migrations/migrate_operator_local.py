"""One-shot migration: operator-local config → ``personal.db`` Settings.

Bead: auto-gko4e (auto-txg5.S4). Required reading:
graph://0d3f750f-f9c (Setting Primitive, Personal Settings section),
graph://d970d946-f95 (Org Registry — role of ``personal.db``),
graph://bcce359d-a1d (Cross-Org Search Architecture — peer subscription
policy).

This is the **last** yaml-retiring migration. It moves two kinds of
operator-local state into Personal Settings in ``personal.db``:

1. ``autonomy.artifact-path#1`` — overrides the default
   ``data/artifacts/{shared|personal}/{org}[/{workspace}]/{name}`` path
   for a given ``<org>:<artifact-name>`` pair. Operators who keep their
   license file, SSH key, etc. somewhere other than the layering default.

2. ``autonomy.org.peer-subscription#1`` — per-caller-org list of peer
   slugs whose public surface the caller sees. Empty list = full
   isolation; an **omitted** Setting means the default "subscribe to
   every peer". Absence and empty list are not equivalent.

Input shape (yaml, additive — today's ``agents/projects.yaml`` has none
of these; tests and operators with non-default setups populate them):

.. code-block:: yaml

    artifact_paths:
      anchore:
        license.yaml: /home/op/licenses/anchore-license.yaml
        id_ed25519: /home/op/.ssh/id_ed25519
      autonomy:
        some-artifact: /elsewhere/some-artifact

    peer_subscriptions:
      autonomy:
        peers: [personal]
      personal:
        peers: []     # fully isolated

Idempotent: re-running with the same yaml is a no-op. A Setting is
considered already migrated when a non-excluded row with the same
``(set_id, key)`` exists in ``personal.db``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from tools.graph import org_ops, schemas
from tools.graph.db import GraphDB
from tools.graph.org_ops import uuid7
from tools.graph.schemas.artifact_path import (
    SET_ID as ARTIFACT_PATH_SET_ID,
    SCHEMA_REVISION as ARTIFACT_PATH_REVISION,
)
from tools.graph.schemas.org_peer_subscription import (
    SET_ID as PEER_SUB_SET_ID,
    SCHEMA_REVISION as PEER_SUB_REVISION,
)


REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ORGS_DIR = REPO_ROOT / "data" / "orgs"
DEFAULT_PROJECTS_YAML = REPO_ROOT / "agents" / "projects.yaml"

PERSONAL_ORG_SLUG = "personal"


# ── Errors ──────────────────────────────────────────────────


class OperatorLocalMigrationError(Exception):
    """Base class for operator-local yaml → Setting migration errors."""


# ── Plan dataclasses ────────────────────────────────────────


@dataclass
class OperatorLocalEntry:
    """One Setting to insert in ``personal.db``."""
    set_id: str
    schema_revision: int
    key: str
    payload: dict
    action: str = "insert"  # 'insert' | 'skip_exists'
    reason: str = ""


@dataclass
class MigrationPlan:
    """What the migration intends to do."""
    yaml_path: Path | None
    personal_db: Path
    entries: list[OperatorLocalEntry] = field(default_factory=list)

    @property
    def inserted(self) -> list[OperatorLocalEntry]:
        return [e for e in self.entries if e.action == "insert"]

    @property
    def skipped(self) -> list[OperatorLocalEntry]:
        return [e for e in self.entries if e.action == "skip_exists"]


# ── Yaml → entries ──────────────────────────────────────────


def _load_yaml(path: Path) -> dict:
    try:
        text = path.read_text()
    except OSError as e:
        raise OperatorLocalMigrationError(f"cannot read yaml: {e}") from e
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise OperatorLocalMigrationError(f"invalid yaml {path}: {e}") from e
    if not isinstance(data, dict):
        raise OperatorLocalMigrationError(
            f"{path}: top-level must be a mapping"
        )
    return data


def _entries_from_artifact_paths(raw: Any) -> list[OperatorLocalEntry]:
    if raw is None:
        return []
    if not isinstance(raw, dict):
        raise OperatorLocalMigrationError(
            "'artifact_paths' must be a mapping of "
            "<org-slug>: {<artifact-name>: <host-path>}"
        )
    out: list[OperatorLocalEntry] = []
    for org, artifacts in raw.items():
        if not isinstance(org, str) or not org:
            raise OperatorLocalMigrationError(
                f"artifact_paths: org key must be a non-empty string, "
                f"got {org!r}"
            )
        if artifacts is None:
            continue
        if not isinstance(artifacts, dict):
            raise OperatorLocalMigrationError(
                f"artifact_paths[{org!r}] must be a mapping of "
                f"<artifact-name>: <host-path>"
            )
        for name, path in artifacts.items():
            if not isinstance(name, str) or not name:
                raise OperatorLocalMigrationError(
                    f"artifact_paths[{org!r}]: artifact name must be a "
                    f"non-empty string, got {name!r}"
                )
            if not isinstance(path, str) or not path:
                raise OperatorLocalMigrationError(
                    f"artifact_paths[{org!r}][{name!r}]: path must be a "
                    f"non-empty string"
                )
            payload = {"path": path}
            schemas.validate_payload(
                ARTIFACT_PATH_SET_ID, ARTIFACT_PATH_REVISION, payload,
            )
            out.append(OperatorLocalEntry(
                set_id=ARTIFACT_PATH_SET_ID,
                schema_revision=ARTIFACT_PATH_REVISION,
                key=f"{org}:{name}",
                payload=payload,
            ))
    return out


def _entries_from_peer_subscriptions(raw: Any) -> list[OperatorLocalEntry]:
    if raw is None:
        return []
    if not isinstance(raw, dict):
        raise OperatorLocalMigrationError(
            "'peer_subscriptions' must be a mapping of "
            "<caller-org>: {peers: [<peer-org>, ...]}"
        )
    out: list[OperatorLocalEntry] = []
    for org, spec in raw.items():
        if not isinstance(org, str) or not org:
            raise OperatorLocalMigrationError(
                f"peer_subscriptions: caller-org key must be a non-empty "
                f"string, got {org!r}"
            )
        if spec is None:
            # Treat a bare `caller: ~` as "isolate fully" for ergonomics
            # — absence from yaml means "use default (all peers)", while
            # explicit null means the operator wrote the key but with no
            # value. Default that to empty list.
            peers: list[str] = []
        elif isinstance(spec, dict):
            raw_peers = spec.get("peers", [])
            if raw_peers is None:
                peers = []
            elif not isinstance(raw_peers, list):
                raise OperatorLocalMigrationError(
                    f"peer_subscriptions[{org!r}].peers must be a list"
                )
            else:
                peers = [str(p) for p in raw_peers]
        else:
            raise OperatorLocalMigrationError(
                f"peer_subscriptions[{org!r}] must be a mapping "
                f"with a 'peers' field"
            )
        payload = {"peers": peers}
        schemas.validate_payload(
            PEER_SUB_SET_ID, PEER_SUB_REVISION, payload,
        )
        out.append(OperatorLocalEntry(
            set_id=PEER_SUB_SET_ID,
            schema_revision=PEER_SUB_REVISION,
            key=org,
            payload=payload,
        ))
    return out


def build_plan(
    yaml_path: Path | str | None = None,
    *,
    orgs_root: Path | str | None = None,
) -> MigrationPlan:
    """Parse *yaml_path* (if given) into a :class:`MigrationPlan`.

    When *yaml_path* is ``None`` (or points at a missing file) the plan
    is empty — useful for operators with no custom paths and no peer
    opt-outs.

    ``orgs_root`` overrides the per-org DB location for tests; honoured
    alongside the ``AUTONOMY_ORGS_DIR`` env var. The personal DB path
    resolves to ``<orgs_root>/personal.db``.
    """
    orgs_dir = _resolve_orgs_dir(orgs_root)
    personal_db = orgs_dir / f"{PERSONAL_ORG_SLUG}.db"
    plan = MigrationPlan(yaml_path=None, personal_db=personal_db)

    if yaml_path is None:
        return plan

    yaml_path = Path(yaml_path)
    plan.yaml_path = yaml_path
    if not yaml_path.exists():
        return plan

    data = _load_yaml(yaml_path)
    plan.entries.extend(_entries_from_artifact_paths(data.get("artifact_paths")))
    plan.entries.extend(_entries_from_peer_subscriptions(
        data.get("peer_subscriptions")
    ))
    return plan


def _resolve_orgs_dir(orgs_root: Path | str | None) -> Path:
    if orgs_root is not None:
        return Path(orgs_root)
    env = os.environ.get("AUTONOMY_ORGS_DIR")
    return Path(env) if env else DEFAULT_ORGS_DIR


# ── Plan → DB ───────────────────────────────────────────────


def _setting_exists(
    db: GraphDB, *, set_id: str, key: str,
) -> bool:
    row = db.conn.execute(
        "SELECT 1 FROM settings WHERE set_id = ? "
        "AND key = ? AND excludes IS NULL LIMIT 1",
        (set_id, key),
    ).fetchone()
    return row is not None


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ensure_personal_db(
    personal_db: Path, orgs_dir: Path,
) -> None:
    """Create ``personal.db`` via :func:`org_ops.ensure_bootstrap_orgs`
    when it doesn't exist. The bootstrap path also seeds the identity
    Setting best-effort.
    """
    if personal_db.exists():
        return
    orgs_dir.mkdir(parents=True, exist_ok=True)
    org_ops.ensure_bootstrap_orgs(root=orgs_dir)


def apply_migration(
    plan: MigrationPlan,
    *,
    dry_run: bool = False,
    state: str = "canonical",
    log=lambda *_a, **_kw: None,
) -> MigrationPlan:
    """Insert Settings per *plan* into ``personal.db``.

    Rows whose ``(set_id, key)`` already exists in the target DB are
    marked ``skip_exists`` and left alone. The plan object is mutated
    in-place and returned for chaining.
    """
    if not plan.entries:
        return plan

    orgs_dir = plan.personal_db.parent
    if not dry_run:
        _ensure_personal_db(plan.personal_db, orgs_dir)

    if dry_run and not plan.personal_db.exists():
        # Can't introspect existing rows without opening the DB. Treat
        # everything as "would insert" and report.
        for entry in plan.entries:
            entry.action = "insert"
        return plan

    db = GraphDB(plan.personal_db)
    try:
        for entry in plan.entries:
            if _setting_exists(db, set_id=entry.set_id, key=entry.key):
                entry.action = "skip_exists"
                entry.reason = (
                    f"{entry.set_id}#{entry.schema_revision} "
                    f"already set for key {entry.key!r}"
                )
                log(f"  {entry.set_id} {entry.key}: skip (exists)")
                continue
            if dry_run:
                entry.action = "insert"
                continue
            sid = uuid7()
            now = _now_iso()
            db.conn.execute(
                "INSERT INTO settings("
                "id, set_id, schema_revision, key, payload, "
                "publication_state, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    sid, entry.set_id, entry.schema_revision,
                    entry.key, json.dumps(entry.payload),
                    state, now, now,
                ),
            )
            entry.action = "insert"
            log(f"  {entry.set_id} {entry.key}: inserted ({sid})")
        if not dry_run:
            db.conn.commit()
    finally:
        db.close()
    return plan


# ── CLI ────────────────────────────────────────────────────


def _print_plan(plan: MigrationPlan) -> None:
    src = plan.yaml_path if plan.yaml_path else "(no yaml)"
    print(f"Migration plan (yaml: {src})")
    print(f"  Personal DB: {plan.personal_db}")
    print()
    if not plan.entries:
        print("  (nothing to migrate — operator has no custom artifact "
              "paths and no peer opt-outs)")
        return
    for entry in plan.entries:
        label = f"{entry.set_id}#{entry.schema_revision}"
        line = f"  {label:<42} key={entry.key!r:<36} ({entry.action})"
        if entry.reason:
            line += f" — {entry.reason}"
        print(line)
    print()
    n_insert = len(plan.inserted)
    n_skip = len(plan.skipped)
    print(f"Summary: {n_insert} to insert, {n_skip} already present")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Migrate operator-local yaml config → personal.db Settings "
            "(auto-gko4e / txg5.S4)."
        ),
    )
    ap.add_argument(
        "--yaml", dest="yaml_path", type=Path,
        default=DEFAULT_PROJECTS_YAML,
        help=(
            f"Path to operator yaml (default: {DEFAULT_PROJECTS_YAML}). "
            f"Missing file is fine — migration is a no-op."
        ),
    )
    ap.add_argument(
        "--orgs-dir", type=Path, default=None,
        help="Per-org DB root (default: AUTONOMY_ORGS_DIR or data/orgs)",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Print the plan and exit without writing",
    )
    ap.add_argument(
        "--state", default="canonical",
        choices=("raw", "curated", "published", "canonical"),
        help="publication_state for inserted Settings (default: canonical)",
    )
    args = ap.parse_args(argv)

    try:
        plan = build_plan(args.yaml_path, orgs_root=args.orgs_dir)
        apply_migration(plan, dry_run=args.dry_run, state=args.state)
        _print_plan(plan)
        return 0
    except OperatorLocalMigrationError as e:
        print(f"MIGRATION FAILED: {e}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    sys.exit(main())
