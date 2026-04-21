"""One-shot migration: yaml artifacts → ``autonomy.workspace.artifact#1`` Settings.

Bead: auto-hhi23 (auto-txg5.S3). Required reading:
graph://0d3f750f-f9c (Setting primitive) and
graph://bc0dda40-f56 (Artifact layering signpost).

For every ``projects.<id>.artifacts[]`` entry in ``agents/projects.yaml``,
insert an ``autonomy.workspace.artifact#1`` Setting in the org DB that the
workspace's ``graph_project`` points at. The Setting's composite key is
``<workspace-id>:<artifact-name>``; the payload carries ``scope``,
``required``, ``description``, and ``help`` — the name and workspace id
live in the key, and file content lives on disk (never in the Setting).

Idempotent: re-running with the same yaml is a no-op. A Setting is
considered already migrated if a non-excluded row exists with the same
``(set_id, key)`` in the target org DB.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..db import GraphDB, _org_db_path
from ..settings_ops import _now_iso
from .. import schemas  # noqa: F401 — side-effect registers workspace_artifact


REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PROJECTS_YAML = REPO_ROOT / "agents" / "projects.yaml"

SET_ID = "autonomy.workspace.artifact"
SCHEMA_REVISION = 1


# ── Errors ───────────────────────────────────────────────────


class ArtifactMigrationError(Exception):
    """Base class for yaml→Setting artifact migration errors."""


# ── Plan / report types ──────────────────────────────────────


@dataclass
class ArtifactEntry:
    """One artifact row to migrate, keyed by (workspace, name, org)."""
    workspace: str
    name: str
    org: str
    payload: dict  # {scope, required, description?, help?}

    @property
    def key(self) -> str:
        return f"{self.workspace}:{self.name}"


@dataclass
class MigrationPlan:
    """What the migration intends to do."""
    yaml_path: Path
    entries: list[ArtifactEntry] = field(default_factory=list)
    # slug -> DB path the slug would land in (display only)
    org_dbs: dict[str, Path] = field(default_factory=dict)


@dataclass
class MigrationReport:
    """Result of :func:`apply_migration`."""
    dry_run: bool
    inserted: int = 0
    already_present: int = 0
    inserted_ids: list[str] = field(default_factory=list)
    # (workspace, name, org) for rows that were skipped as already-present.
    skipped: list[tuple[str, str, str]] = field(default_factory=list)


# ── yaml → plan ──────────────────────────────────────────────


def _payload_from_yaml(raw: dict) -> dict:
    """Derive the Setting payload from a yaml ``artifacts[]`` entry.

    The yaml is the source of truth for the shape; we strip ``name``
    (lives in the Setting key) and any unknown keys. Optional fields
    missing from yaml simply don't appear in the payload.
    """
    payload: dict = {"scope": raw["scope"]}
    if "required" in raw:
        payload["required"] = bool(raw["required"])
    for opt in ("description", "help"):
        if opt in raw and raw[opt] not in (None, ""):
            payload[opt] = str(raw[opt])
    return payload


def build_plan(
    yaml_path: Path | str = DEFAULT_PROJECTS_YAML,
    *,
    orgs_root: Path | str | None = None,
) -> MigrationPlan:
    """Parse *yaml_path* into a :class:`MigrationPlan`.

    ``orgs_root`` overrides the per-org DB location (tests; also
    honoured via :func:`resolve_caller_db_path` + ``AUTONOMY_ORGS_DIR``).
    """
    yaml_path = Path(yaml_path)
    if not yaml_path.exists():
        raise ArtifactMigrationError(f"yaml not found: {yaml_path}")
    try:
        data = yaml.safe_load(yaml_path.read_text()) or {}
    except yaml.YAMLError as e:
        raise ArtifactMigrationError(f"invalid yaml in {yaml_path}: {e}")

    projects = data.get("projects")
    if not isinstance(projects, dict):
        raise ArtifactMigrationError(
            f"{yaml_path}: missing or malformed 'projects' mapping"
        )

    plan = MigrationPlan(yaml_path=yaml_path)
    for workspace_id, project in projects.items():
        if not isinstance(project, dict):
            continue
        org = project.get("graph_project")
        if not isinstance(org, str) or not org:
            raise ArtifactMigrationError(
                f"project {workspace_id!r}: 'graph_project' is required"
            )
        artifacts = project.get("artifacts") or []
        if not isinstance(artifacts, list):
            raise ArtifactMigrationError(
                f"project {workspace_id!r}: 'artifacts' must be a list"
            )
        if not artifacts:
            continue
        for idx, raw in enumerate(artifacts):
            if not isinstance(raw, dict):
                raise ArtifactMigrationError(
                    f"project {workspace_id!r}: artifacts[{idx}] must be a mapping"
                )
            name = raw.get("name")
            if not isinstance(name, str) or not name:
                raise ArtifactMigrationError(
                    f"project {workspace_id!r}: artifacts[{idx}] missing 'name'"
                )
            if "scope" not in raw:
                raise ArtifactMigrationError(
                    f"project {workspace_id!r}: artifacts[{idx}] missing 'scope'"
                )
            plan.entries.append(ArtifactEntry(
                workspace=str(workspace_id),
                name=name,
                org=org,
                payload=_payload_from_yaml(raw),
            ))
        plan.org_dbs.setdefault(org, _resolve_org_db(org, orgs_root))
    return plan


def _resolve_org_db(slug: str, orgs_root: Path | str | None) -> Path:
    """Return the per-org DB path the migration will write to.

    Unlike :func:`resolve_caller_db_path`, this never falls back to
    ``DEFAULT_DB`` when the per-org DB doesn't yet exist — the migration
    *creates* that file. Settings from yaml always land in ``<slug>.db``
    even when it has to be materialised.

    ``orgs_root`` (test override) and ``AUTONOMY_ORGS_DIR`` both route the
    per-org filesystem layout; ``GRAPH_DB`` is deliberately ignored here
    because the migration's purpose is to populate per-org DBs, not to
    squash every slug into a single pinned file.
    """
    return _org_db_path(slug, orgs_root)


# ── plan → db ────────────────────────────────────────────────


def _existing_key_ids(db: GraphDB, key: str) -> list[str]:
    rows = db.conn.execute(
        "SELECT id FROM settings WHERE set_id = ? AND key = ? "
        "AND excludes IS NULL",
        (SET_ID, key),
    ).fetchall()
    return [r["id"] for r in rows]


def _insert_setting(
    db: GraphDB,
    entry: ArtifactEntry,
    *,
    state: str = "canonical",
) -> str:
    schemas.validate_payload(SET_ID, SCHEMA_REVISION, entry.payload)
    from uuid import uuid4
    sid = str(uuid4())
    now = _now_iso()
    db.conn.execute(
        "INSERT INTO settings(id, set_id, schema_revision, key, payload, "
        "publication_state, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (sid, SET_ID, SCHEMA_REVISION, entry.key,
         json.dumps(entry.payload), state, now, now),
    )
    return sid


def apply_migration(
    plan: MigrationPlan,
    *,
    dry_run: bool = False,
    state: str = "canonical",
) -> MigrationReport:
    """Insert Settings per *plan*.

    Opens one GraphDB per distinct org. Rows whose ``(set_id, key)``
    already exists in the target DB are recorded in ``skipped`` and left
    alone — callers who want to replace an entry should remove it first.
    """
    report = MigrationReport(dry_run=dry_run)
    dbs: dict[str, GraphDB] = {}
    try:
        for entry in plan.entries:
            db = dbs.get(entry.org)
            if db is None:
                db = GraphDB(plan.org_dbs[entry.org])
                dbs[entry.org] = db
            if _existing_key_ids(db, entry.key):
                report.already_present += 1
                report.skipped.append((entry.workspace, entry.name, entry.org))
                continue
            if dry_run:
                report.inserted += 1
                continue
            sid = _insert_setting(db, entry, state=state)
            report.inserted += 1
            report.inserted_ids.append(sid)
        if not dry_run:
            for db in dbs.values():
                db.conn.commit()
    finally:
        for db in dbs.values():
            db.close()
    return report


# ── CLI entry point ──────────────────────────────────────────


def _format_report(plan: MigrationPlan, report: MigrationReport) -> str:
    mode = "DRY RUN" if report.dry_run else "WRITE"
    lines = [
        f"[{mode}] migrate {plan.yaml_path} → {SET_ID}#{SCHEMA_REVISION}",
        f"  entries planned:     {len(plan.entries)}",
        f"  inserted:            {report.inserted}",
        f"  already present:     {report.already_present}",
    ]
    if plan.org_dbs:
        lines.append("  orgs targeted:")
        for slug, path in sorted(plan.org_dbs.items()):
            lines.append(f"    {slug:<16} → {path}")
    if report.skipped:
        lines.append("  skipped (already present):")
        for ws, name, org in report.skipped[:20]:
            lines.append(f"    {org}:{ws}:{name}")
        if len(report.skipped) > 20:
            lines.append(f"    ... ({len(report.skipped) - 20} more)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--yaml", dest="yaml_path",
        default=str(DEFAULT_PROJECTS_YAML),
        help="Path to projects.yaml (default: agents/projects.yaml)",
    )
    parser.add_argument(
        "--orgs-dir", dest="orgs_dir", default=None,
        help="Override data/orgs/ root (tests)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", dest="dry_run",
        help="Print the plan without touching any DB",
    )
    parser.add_argument(
        "--state", default="canonical",
        choices=("raw", "curated", "published", "canonical"),
        help="publication_state for inserted Settings (default: canonical)",
    )
    args = parser.parse_args(argv)

    try:
        plan = build_plan(args.yaml_path, orgs_root=args.orgs_dir)
        report = apply_migration(
            plan, dry_run=args.dry_run, state=args.state,
        )
    except ArtifactMigrationError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2

    print(_format_report(plan, report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
