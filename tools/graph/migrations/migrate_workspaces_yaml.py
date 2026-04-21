"""One-shot migration: ``agents/projects.yaml`` → ``autonomy.workspace#1``.

Spec: graph://0d3f750f-f9c (Setting Primitive) + graph://eabec73c-baa
(Workspaces & Orgs). Bead: auto-raycq (txg5.S2).

For each ``projects:<id>:`` entry in ``agents/projects.yaml``, insert a
``autonomy.workspace#1`` Setting into the owning org's DB. The owning
org is determined by the yaml's ``graph_project:`` value; the resulting
Setting has ``key=<id>`` and ``state=canonical``.

Fields stripped before write (carried by other layers):

* ``graph_project`` — implicit from which DB the Setting lives in.
  Also renamed to ``graph_org`` and dropped entirely in auto-0wj9.
* ``artifacts`` — owned by ``autonomy.workspace.artifact#1`` (auto-S3).

The migration is idempotent: rows already present (matching
``set_id + key + schema_revision`` in the target DB) are skipped.

Run with ``--dry-run`` to print the plan without touching any DB. Env
var ``AUTONOMY_ORGS_DIR`` overrides the per-org DB root (same as
auto-9iq2s).
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

from tools.graph.db import GraphDB
from tools.graph import schemas
from tools.graph.org_ops import uuid7


REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ORGS_DIR = REPO_ROOT / "data" / "orgs"
DEFAULT_PROJECTS_YAML = REPO_ROOT / "agents" / "projects.yaml"

# Fields stripped from yaml before writing the Setting payload.
_YAML_FIELDS_DROP = ("graph_project", "artifacts")


# ── Errors ──────────────────────────────────────────────────


class WorkspaceMigrationError(Exception):
    """Base class for migration aborts."""


class MissingOrgDBError(WorkspaceMigrationError):
    """A workspace's ``graph_project:`` org has no DB yet.

    Migration should be run *after* per-org DBs exist (auto-9iq2s already
    creates one per distinct ``graph_project`` value).
    """


# ── Plan dataclass ──────────────────────────────────────────


@dataclass
class WorkspacePlan:
    """Per-workspace migration plan entry."""
    workspace_id: str
    org_slug: str
    org_db: Path
    payload: dict
    action: str = "insert"  # 'insert' | 'skip_exists' | 'error'
    reason: str = ""


@dataclass
class MigrationReport:
    yaml_path: Path
    orgs_dir: Path
    entries: list[WorkspacePlan] = field(default_factory=list)

    @property
    def inserted(self) -> list[WorkspacePlan]:
        return [e for e in self.entries if e.action == "insert"]

    @property
    def skipped(self) -> list[WorkspacePlan]:
        return [e for e in self.entries if e.action == "skip_exists"]


# ── Yaml → payload ─────────────────────────────────────────


def _build_payload(raw: dict) -> dict:
    """Project ``projects:<id>:`` dict onto the ``autonomy.workspace#1``
    payload shape.

    Drops ``graph_project`` (implicit) and ``artifacts`` (handled by
    auto-S3). Drops keys whose value is ``None`` (yaml omission) so we
    don't persist noise like ``working_dir: null``.
    """
    payload: dict[str, Any] = {}
    for k, v in raw.items():
        if k in _YAML_FIELDS_DROP:
            continue
        if v is None:
            continue
        if k == "default_tags":
            # Renamed to 'tags' in the setting schema (auto-rruc).
            payload["tags"] = list(v)
            continue
        if k == "repos":
            payload["repos"] = [_normalize_repo(r) for r in v]
            continue
        if k == "env":
            payload["env"] = {str(kk): str(vv) for kk, vv in v.items()}
            continue
        if k in ("env_from_host", "tags", "dispatch_labels"):
            payload[k] = [str(x) for x in v]
            continue
        payload[k] = v
    return payload


def _normalize_repo(repo: Any) -> dict:
    if not isinstance(repo, dict):
        raise WorkspaceMigrationError(
            f"repo entry must be a mapping, got {type(repo).__name__}"
        )
    out: dict[str, Any] = {
        "url": str(repo["url"]),
        "mount": str(repo["mount"]),
    }
    if "writable" in repo:
        out["writable"] = bool(repo["writable"])
    return out


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


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Plan ──────────────────────────────────────────────────


def load_yaml(path: Path) -> dict:
    try:
        text = path.read_text()
    except OSError as e:
        raise WorkspaceMigrationError(f"cannot read yaml: {e}") from e
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise WorkspaceMigrationError(f"invalid yaml {path}: {e}") from e
    if not isinstance(data, dict):
        raise WorkspaceMigrationError(
            f"{path}: top-level must be a mapping"
        )
    return data


def build_plan(
    yaml_path: Path, orgs_dir: Path,
) -> MigrationReport:
    """Inspect yaml + per-org DBs; produce the per-workspace plan."""
    data = load_yaml(yaml_path)
    projects = data.get("projects") or {}
    if not isinstance(projects, dict):
        raise WorkspaceMigrationError(
            f"{yaml_path}: 'projects' must be a mapping"
        )

    report = MigrationReport(yaml_path=yaml_path, orgs_dir=orgs_dir)
    for workspace_id, raw in projects.items():
        if not isinstance(raw, dict):
            raise WorkspaceMigrationError(
                f"projects[{workspace_id!r}] must be a mapping"
            )
        org_slug = raw.get("graph_project")
        if not isinstance(org_slug, str) or not org_slug:
            raise WorkspaceMigrationError(
                f"projects[{workspace_id!r}]: 'graph_project' is "
                f"required and must be a non-empty string"
            )
        org_db = orgs_dir / f"{org_slug}.db"
        payload = _build_payload(raw)

        # Validate payload shape up front so the operator sees every
        # offending workspace before any write, not the first insert error.
        schemas.validate_payload("autonomy.workspace", 1, payload)

        entry = WorkspacePlan(
            workspace_id=str(workspace_id),
            org_slug=org_slug,
            org_db=org_db,
            payload=payload,
        )
        if not org_db.exists():
            entry.action = "error"
            entry.reason = (
                f"org DB not found: {org_db}. Run auto-9iq2s first."
            )
            report.entries.append(entry)
            continue

        db = GraphDB(org_db)
        try:
            if _setting_exists(
                db, set_id="autonomy.workspace",
                schema_revision=1, key=str(workspace_id),
            ):
                entry.action = "skip_exists"
                entry.reason = "autonomy.workspace#1 already set"
        finally:
            db.close()
        report.entries.append(entry)

    return report


# ── Apply ─────────────────────────────────────────────────


def apply_migration(
    report: MigrationReport, *, log=print,
) -> MigrationReport:
    """Execute the plan. Skips entries already marked ``skip_exists`` or
    ``error``. Returns the same report for chaining.
    """
    errors = [e for e in report.entries if e.action == "error"]
    if errors:
        details = "\n".join(
            f"  {e.workspace_id}: {e.reason}" for e in errors
        )
        raise MissingOrgDBError(
            f"cannot apply migration; {len(errors)} workspace(s) "
            f"have no org DB:\n{details}"
        )

    for entry in report.entries:
        if entry.action != "insert":
            continue
        db = GraphDB(entry.org_db)
        try:
            # Re-check inside the write to stay idempotent under
            # concurrent runs (belt + suspenders).
            if _setting_exists(
                db, set_id="autonomy.workspace",
                schema_revision=1, key=entry.workspace_id,
            ):
                entry.action = "skip_exists"
                entry.reason = "raced — already present"
                log(f"  {entry.org_slug}/{entry.workspace_id}: skip (exists)")
                continue
            sid = uuid7()
            now = _now_iso()
            db.conn.execute(
                "INSERT INTO settings("
                "id, set_id, schema_revision, key, payload, "
                "publication_state, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    sid, "autonomy.workspace", 1, entry.workspace_id,
                    json.dumps(entry.payload), "canonical", now, now,
                ),
            )
            db.conn.commit()
            log(f"  {entry.org_slug}/{entry.workspace_id}: inserted ({sid})")
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
            f"  {entry.workspace_id:<20} → {entry.org_slug:<12} "
            f"({entry.action})"
        )
        if entry.reason:
            line += f" — {entry.reason}"
        print(line)
    print()
    n_insert = sum(1 for e in report.entries if e.action == "insert")
    n_skip = sum(1 for e in report.entries if e.action == "skip_exists")
    n_err = sum(1 for e in report.entries if e.action == "error")
    print(
        f"Summary: {n_insert} to insert, {n_skip} already present, "
        f"{n_err} error(s)"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Migrate agents/projects.yaml workspaces → "
            "autonomy.workspace#1 Settings (auto-raycq / txg5.S2)."
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
    except MissingOrgDBError as e:
        print(f"MIGRATION FAILED: {e}", file=sys.stderr)
        return 3
    except WorkspaceMigrationError as e:
        print(f"MIGRATION FAILED: {e}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    sys.exit(main())
