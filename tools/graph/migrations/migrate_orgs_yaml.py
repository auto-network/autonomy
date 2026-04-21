"""One-shot migration: ``agents/projects.yaml`` orgs → ``autonomy.org#1``.

Spec: graph://0d3f750f-f9c (Setting Primitive) + graph://d970d946-f95
(Org Registry) + graph://497cdc20-d43 (Identity asset cascade).
Bead: auto-4a1cn (txg5.S1).

For each top-level ``orgs:<slug>:`` entry in ``agents/projects.yaml``,
insert an ``autonomy.org#1`` Setting into ``data/orgs/<slug>.db``, keyed
by the slug, with ``state=canonical``. When the org's per-org DB does
not yet exist the migration creates it (auto-vkfyi bootstrap path).

Type inference: the yaml ``orgs:`` schema doesn't carry a ``type``
field. If the per-org DB already exists, the migration reads the
bootstrap row's type. Otherwise it defaults to ``personal`` for the
``personal`` slug and ``shared`` for everything else — matching
``ensure_bootstrap_orgs``.

The migration is idempotent: rows already present (matching
``set_id + key + schema_revision`` in the target DB) are skipped.

Run with ``--dry-run`` to print the plan without touching any DB. Env
var ``AUTONOMY_ORGS_DIR`` overrides the per-org DB root.
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
from tools.graph.schemas.org import ORG_SET_ID, ORG_REVISION


REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ORGS_DIR = REPO_ROOT / "data" / "orgs"
DEFAULT_PROJECTS_YAML = REPO_ROOT / "agents" / "projects.yaml"


# ── Errors ──────────────────────────────────────────────────


class OrgMigrationError(Exception):
    """Base class for migration aborts."""


# ── Plan dataclasses ───────────────────────────────────────


@dataclass
class OrgPlan:
    """Per-org migration plan entry."""
    slug: str
    org_db: Path
    payload: dict
    org_type: str
    action: str = "insert"  # 'insert' | 'skip_exists' | 'create_db_and_insert'
    reason: str = ""


@dataclass
class MigrationReport:
    yaml_path: Path
    orgs_dir: Path
    entries: list[OrgPlan] = field(default_factory=list)

    @property
    def inserted(self) -> list[OrgPlan]:
        return [e for e in self.entries if e.action.endswith("insert")]

    @property
    def skipped(self) -> list[OrgPlan]:
        return [e for e in self.entries if e.action == "skip_exists"]


# ── Yaml → payload ─────────────────────────────────────────


def _build_payload(slug: str, raw: dict | None, org_type: str) -> dict:
    """Project ``orgs:<slug>:`` dict onto the ``autonomy.org#1`` payload.

    Drops keys whose value is ``None`` (yaml omission). Injects the
    inferred ``type`` so the Setting carries everything renderers need
    without a second lookup.
    """
    if raw is None:
        raw = {}
    payload: dict[str, Any] = {}
    # Per schema contract, 'name' is required — fall back to slug so
    # operator-shorthand yaml (e.g. ``personal: {color: "#A0A0A0"}``)
    # doesn't need a redundant name.
    name = raw.get("name")
    if not (isinstance(name, str) and name):
        name = slug
    payload["name"] = name
    for k in ("byline", "color", "favicon"):
        v = raw.get(k)
        if v is None:
            continue
        payload[k] = str(v)
    payload["type"] = org_type
    return payload


# ── Lookup helpers ─────────────────────────────────────────


def _setting_exists(
    db: GraphDB, *, set_id: str, schema_revision: int, key: str,
) -> bool:
    row = db.conn.execute(
        "SELECT 1 FROM settings WHERE set_id = ? "
        "AND schema_revision = ? AND key = ? LIMIT 1",
        (set_id, int(schema_revision), key),
    ).fetchone()
    return row is not None


def _read_bootstrap_type(db_path: Path) -> str | None:
    """Read the bootstrap ``orgs`` row's type. Returns ``None`` when the
    file is absent — callers must not treat a missing file as "create".
    Opening ``GraphDB`` on a non-existent path would create an empty
    SQLite file as a side effect, which would then mask the "DB missing"
    branch in :func:`build_plan`.
    """
    if not db_path.exists():
        return None
    try:
        db = GraphDB(db_path)
    except Exception:
        return None
    try:
        row = db.conn.execute(
            "SELECT type FROM orgs LIMIT 1"
        ).fetchone()
    except Exception:
        return None
    finally:
        db.close()
    if row is None:
        return None
    t = row["type"]
    return t if t in org_ops.VALID_ORG_TYPES else None


def _infer_type(slug: str, db_path: Path) -> str:
    """Infer the org type.

    Priority: existing bootstrap row > slug convention ('personal' →
    personal, else shared). This matches ``ensure_bootstrap_orgs``.
    """
    existing = _read_bootstrap_type(db_path)
    if existing is not None:
        return existing
    return "personal" if slug == "personal" else "shared"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Plan ──────────────────────────────────────────────────


def load_yaml(path: Path) -> dict:
    try:
        text = path.read_text()
    except OSError as e:
        raise OrgMigrationError(f"cannot read yaml: {e}") from e
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise OrgMigrationError(f"invalid yaml {path}: {e}") from e
    if not isinstance(data, dict):
        raise OrgMigrationError(
            f"{path}: top-level must be a mapping"
        )
    return data


def build_plan(
    yaml_path: Path, orgs_dir: Path,
) -> MigrationReport:
    """Inspect yaml + per-org DBs; produce the per-org plan."""
    data = load_yaml(yaml_path)
    orgs = data.get("orgs") or {}
    if not isinstance(orgs, dict):
        raise OrgMigrationError(
            f"{yaml_path}: 'orgs' must be a mapping"
        )

    report = MigrationReport(yaml_path=yaml_path, orgs_dir=orgs_dir)
    for slug, raw in orgs.items():
        slug = str(slug)
        if raw is not None and not isinstance(raw, dict):
            raise OrgMigrationError(
                f"orgs[{slug!r}] must be a mapping or null"
            )
        org_db = orgs_dir / f"{slug}.db"
        org_type = _infer_type(slug, org_db)
        payload = _build_payload(slug, raw, org_type)

        # Validate up front so every offending entry surfaces before writes.
        schemas.validate_payload(ORG_SET_ID, ORG_REVISION, payload)

        entry = OrgPlan(
            slug=slug, org_db=org_db, payload=payload, org_type=org_type,
        )
        if not org_db.exists():
            entry.action = "create_db_and_insert"
            entry.reason = "org DB will be created"
            report.entries.append(entry)
            continue

        db = GraphDB(org_db)
        try:
            if _setting_exists(
                db, set_id=ORG_SET_ID,
                schema_revision=ORG_REVISION, key=slug,
            ):
                entry.action = "skip_exists"
                entry.reason = "autonomy.org#1 already set"
        finally:
            db.close()
        report.entries.append(entry)

    return report


# ── Apply ─────────────────────────────────────────────────


def apply_migration(
    report: MigrationReport, *, log=print,
) -> MigrationReport:
    """Execute the plan. Skips entries marked ``skip_exists``."""
    report.orgs_dir.mkdir(parents=True, exist_ok=True)
    for entry in report.entries:
        if entry.action == "skip_exists":
            continue

        if entry.action == "create_db_and_insert":
            # Let org_ops.create_org handle DB creation + bootstrap row +
            # identity seed in a single step. The seed path runs because
            # autonomy.org#1 is registered (this bead's schema.org module).
            try:
                org_ops.create_org(
                    entry.slug,
                    type_=entry.org_type,
                    identity_payload=entry.payload,
                    identity_state="canonical",
                    root=report.orgs_dir,
                )
            except org_ops.OrgExistsError:
                # Race: another runner created the DB between plan + apply.
                # Fall through to the normal insert path.
                pass
            else:
                log(f"  {entry.slug}: created DB + inserted identity")
                continue

        db = GraphDB(entry.org_db)
        try:
            # Re-check inside the write to stay idempotent under
            # concurrent runs (belt + suspenders).
            if _setting_exists(
                db, set_id=ORG_SET_ID,
                schema_revision=ORG_REVISION, key=entry.slug,
            ):
                entry.action = "skip_exists"
                entry.reason = "raced — already present"
                log(f"  {entry.slug}: skip (exists)")
                continue
            sid = uuid7()
            now = _now_iso()
            db.conn.execute(
                "INSERT INTO settings("
                "id, set_id, schema_revision, key, payload, "
                "publication_state, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    sid, ORG_SET_ID, ORG_REVISION, entry.slug,
                    json.dumps(entry.payload), "canonical", now, now,
                ),
            )
            db.conn.commit()
            log(f"  {entry.slug}: inserted ({sid})")
        finally:
            db.close()
    return report


# ── CLI ──────────────────────────────────────────────────


def _print_plan(report: MigrationReport) -> None:
    print(f"Migration plan (yaml: {report.yaml_path})")
    print(f"  Per-org dir: {report.orgs_dir}")
    print()
    for entry in report.entries:
        line = (
            f"  {entry.slug:<16} type={entry.org_type:<8} "
            f"({entry.action})"
        )
        if entry.reason:
            line += f" — {entry.reason}"
        print(line)
    print()
    n_insert = sum(
        1 for e in report.entries
        if e.action in ("insert", "create_db_and_insert")
    )
    n_skip = sum(1 for e in report.entries if e.action == "skip_exists")
    print(
        f"Summary: {n_insert} to insert, {n_skip} already present"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Migrate agents/projects.yaml orgs → "
            "autonomy.org#1 Settings (auto-4a1cn / txg5.S1)."
        ),
    )
    ap.add_argument(
        "--projects-yaml", type=Path, default=DEFAULT_PROJECTS_YAML,
        help=f"Path to projects.yaml (default: {DEFAULT_PROJECTS_YAML})",
    )
    ap.add_argument(
        "--orgs-dir", type=Path, default=None,
        help="Per-org DB root (default: AUTONOMY_ORGS_DIR or data/orgs)",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Print the plan and exit without writing",
    )
    args = ap.parse_args(argv)

    orgs_dir = args.orgs_dir
    if orgs_dir is None:
        env = os.environ.get("AUTONOMY_ORGS_DIR")
        orgs_dir = Path(env) if env else DEFAULT_ORGS_DIR

    try:
        report = build_plan(args.projects_yaml, orgs_dir)
        _print_plan(report)
        if args.dry_run:
            return 0
        apply_migration(report)
        print()
        print(
            f"Migration OK: {len(report.inserted)} inserted, "
            f"{len(report.skipped)} skipped"
        )
        return 0
    except OrgMigrationError as e:
        print(f"MIGRATION FAILED: {e}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    sys.exit(main())
