"""One-shot migration: split ``data/graph.db`` into per-org DBs.

Spec: graph://7c296600-19b (per-org DB decision) + graph://bcce359d-a1d
(cross-org search architecture). Bead: auto-9iq2s (txg5.2).

Partitions every row in the legacy single ``data/graph.db`` into
``data/orgs/<slug>.db`` based on each row's home org:

* ``sources`` rows partition by their ``project`` column. NULL or
  unknown values land in ``autonomy`` (the platform default per
  auto-s45z9 default-scope convergence) with a logged warning for
  unknown values.
* Per-source content (``thoughts``, ``derivations``, ``claims``,
  ``edges``, ``attachments``, ``note_comments``, ``note_versions``,
  ``note_reads``, ``captures``, ``entity_mentions``) follows its
  source's destination.
* ``tags`` are global vocabulary — duplicated to every org DB.
* ``entities`` are copied to every org that references them via
  ``entity_mentions``; entity-less orgs get none.
* Operator-local / global tables (``threads``, ``nodes``,
  ``node_refs``, ``settings``) land in ``autonomy.db`` for now.

Pre-flight refuses to run unless:

1. ``data/dashboard.pid`` and ``data/dispatcher.pid`` are absent or
   point at non-running processes.
2. The backup of ``data/graph.db`` (and any WAL/SHM siblings) succeeds.

Idempotent: if ``data/orgs/autonomy.db`` already has > 0 sources AND
``data/graph.db.legacy-*`` exists, the script refuses politely.

Run with ``--dry-run`` to print the partition plan without touching
any DB. The ``--root`` flag overrides the legacy DB location for
tests; ``--orgs-dir`` overrides the per-org root (also honoured via
``AUTONOMY_ORGS_DIR``).
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import shutil
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_LEGACY_DB = REPO_ROOT / "data" / "graph.db"
DEFAULT_ORGS_DIR = REPO_ROOT / "data" / "orgs"
DEFAULT_PROJECTS_YAML = REPO_ROOT / "agents" / "projects.yaml"
DEFAULT_PID_DIR = REPO_ROOT / "data"

# Orgs that always exist after migration regardless of yaml content.
# `autonomy` is the platform default; `personal` is the operator-local bucket.
BASELINE_ORGS = ("autonomy", "personal")
DEFAULT_FALLBACK_ORG = "autonomy"


# ── Errors ───────────────────────────────────────────────────


class MigrationError(Exception):
    """Base class for migration aborts (caught and reported by main)."""


class PreflightError(MigrationError):
    """Pre-flight check failed (running services, missing legacy DB, …)."""


class AlreadyMigratedError(MigrationError):
    """Per-org DBs already present — refuse to clobber."""


# ── Plan dataclasses ─────────────────────────────────────────


@dataclass
class OrgPartition:
    """How many rows of each table land in a given org."""
    slug: str
    sources: int = 0
    thoughts: int = 0
    derivations: int = 0
    edges: int = 0
    claims: int = 0
    entity_mentions: int = 0
    entities: int = 0
    note_comments: int = 0
    note_versions: int = 0
    note_reads: int = 0
    attachments: int = 0
    captures: int = 0
    threads: int = 0
    nodes: int = 0
    node_refs: int = 0
    tags: int = 0
    settings: int = 0


@dataclass
class MigrationPlan:
    legacy_db: Path
    orgs_dir: Path
    org_slugs: list[str]
    source_org: dict[str, str]  # source_id -> org slug
    null_project_count: int = 0
    unknown_project_count: int = 0
    unknown_projects: dict[str, int] = field(default_factory=dict)
    partitions: dict[str, OrgPartition] = field(default_factory=dict)
    # Per-table orphan counts: rows with source_id that no longer has a
    # matching sources row. Populated by build_plan so the operator can
    # see the data-loss surface before applying, and so verify_migration
    # can subtract from the expected legacy totals.
    orphans_by_table: dict[str, int] = field(default_factory=dict)


# ── Pre-flight ───────────────────────────────────────────────


def _pid_alive(pid: int) -> bool:
    """True if a process with that pid is currently running."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as e:
        if e.errno == errno.ESRCH:
            return False
        # EPERM — process exists, we just can't signal it
        return e.errno == errno.EPERM
    return True


def check_services_stopped(pid_dir: Path = DEFAULT_PID_DIR) -> None:
    """Refuse to migrate while dashboard or dispatcher are running."""
    for service in ("dashboard", "dispatcher"):
        pid_file = pid_dir / f"{service}.pid"
        if not pid_file.exists():
            continue
        try:
            pid = int(pid_file.read_text().strip())
        except (ValueError, OSError):
            continue
        if _pid_alive(pid):
            raise PreflightError(
                f"{service} appears to be running (pid {pid} from "
                f"{pid_file}); stop it before migrating."
            )


def check_idempotency(orgs_dir: Path, legacy_db: Path) -> None:
    """Refuse if a previous migration already wrote per-org DBs.

    Detection: ``data/orgs/autonomy.db`` exists with > 0 ``sources``
    rows AND a ``data/graph.db.legacy-*`` backup is present. Both
    must be true so a partial bring-up doesn't false-positive.
    """
    autonomy_db = orgs_dir / "autonomy.db"
    has_autonomy = False
    if autonomy_db.exists():
        try:
            conn = sqlite3.connect(f"file:{autonomy_db}?mode=ro", uri=True)
            try:
                row = conn.execute(
                    "SELECT COUNT(*) FROM sources"
                ).fetchone()
                has_autonomy = (row[0] or 0) > 0
            finally:
                conn.close()
        except sqlite3.Error:
            has_autonomy = False
    # Glob matches the rename + any -wal/-shm siblings; collapse to the
    # bare DB filename(s) so the error message names the canonical file.
    legacy_backups = sorted(
        p for p in legacy_db.parent.glob(f"{legacy_db.name}.legacy-*")
        if not p.name.endswith(("-wal", "-shm"))
    )
    if has_autonomy and legacy_backups:
        raise AlreadyMigratedError(
            "migration already complete; "
            f"{autonomy_db} has rows and "
            f"{legacy_backups[0].name} exists. "
            "To re-run, restore from the backup and "
            "remove data/orgs/."
        )


def backup_legacy_db(legacy_db: Path, *, timestamp: str) -> list[Path]:
    """Copy ``data/graph.db`` (+ WAL/SHM) to ``.pre-txg5-<ts>`` siblings.

    Returns the list of files written. Raises :class:`PreflightError`
    on any copy failure (the migration must not proceed without an
    intact backup).
    """
    written: list[Path] = []
    if not legacy_db.exists():
        raise PreflightError(f"legacy DB missing: {legacy_db}")
    for suffix in ("", "-wal", "-shm"):
        src = legacy_db.parent / f"{legacy_db.name}{suffix}"
        if not src.exists():
            continue
        dst = legacy_db.parent / f"{legacy_db.name}.pre-txg5-{timestamp}{suffix}"
        try:
            shutil.copy2(src, dst)
        except OSError as e:
            # Roll back any partial backup so we don't leave litter.
            for w in written:
                try:
                    w.unlink()
                except OSError:
                    pass
            raise PreflightError(
                f"backup failed for {src}: {e}"
            ) from e
        written.append(dst)
    if not written:
        raise PreflightError(
            f"no files copied — legacy DB unexpectedly empty: {legacy_db}"
        )
    return written


# ── Org enumeration ──────────────────────────────────────────


def enumerate_target_orgs(
    yaml_path: Path | None = None,
    *,
    extra: Iterable[str] = (),
) -> list[str]:
    """Return the deduplicated, sorted list of orgs to create.

    Sources:
      1. Baseline (``autonomy``, ``personal``) — always present.
      2. Distinct ``graph_project`` values in ``agents/projects.yaml``.
      3. Caller-supplied extras (e.g. orphaned project names from
         the legacy DB).

    YAML parsing is intentionally minimal and tolerant — we only need
    the ``graph_project:`` values, and a missing file just falls back
    to the baseline + extras.
    """
    slugs: set[str] = set(BASELINE_ORGS)
    slugs.update(extra)
    if yaml_path is not None and yaml_path.exists():
        try:
            text = yaml_path.read_text()
        except OSError:
            text = ""
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("graph_project:"):
                value = stripped.split(":", 1)[1].strip()
                # Strip quotes and inline comments.
                if "#" in value:
                    value = value.split("#", 1)[0].strip()
                value = value.strip("'").strip('"').strip()
                if value:
                    slugs.add(value)
    return sorted(slugs)


# ── Plan ────────────────────────────────────────────────────


def _open_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def build_plan(
    legacy_db: Path,
    orgs_dir: Path,
    yaml_path: Path | None = DEFAULT_PROJECTS_YAML,
) -> MigrationPlan:
    """Inspect the legacy DB and decide where every source row lands.

    Builds a :class:`MigrationPlan` with per-org row counts so the
    operator can see the partition shape before the script touches
    anything (``--dry-run``).
    """
    if not legacy_db.exists():
        raise PreflightError(f"legacy DB missing: {legacy_db}")

    conn = _open_ro(legacy_db)
    try:
        # Distinct projects in the legacy DB — including orphaned values
        # that don't appear in yaml.
        legacy_projects = {
            r[0] for r in conn.execute(
                "SELECT DISTINCT project FROM sources"
            ).fetchall()
        }
        legacy_projects.discard(None)

        target_orgs = enumerate_target_orgs(yaml_path)
        unknown = legacy_projects - set(target_orgs)
        # Unknowns route to autonomy with a logged warning; we do NOT
        # silently mint a new org for them.

        source_org: dict[str, str] = {}
        null_count = 0
        unknown_counts: dict[str, int] = {}

        rows = conn.execute(
            "SELECT id, project FROM sources"
        ).fetchall()
        for r in rows:
            sid, project = r["id"], r["project"]
            if project is None:
                null_count += 1
                source_org[sid] = DEFAULT_FALLBACK_ORG
            elif project in unknown:
                unknown_counts[project] = unknown_counts.get(project, 0) + 1
                source_org[sid] = DEFAULT_FALLBACK_ORG
            else:
                source_org[sid] = project

        plan = MigrationPlan(
            legacy_db=legacy_db,
            orgs_dir=orgs_dir,
            org_slugs=target_orgs,
            source_org=source_org,
            null_project_count=null_count,
            unknown_project_count=sum(unknown_counts.values()),
            unknown_projects=unknown_counts,
        )
        for slug in target_orgs:
            plan.partitions[slug] = OrgPartition(slug=slug)

        # Per-org source counts.
        for sid, slug in source_org.items():
            plan.partitions[slug].sources += 1

        # thoughts/derivations/edges/etc — count by joining to sources.
        # We cannot rely on a real join (some edge endpoints are not
        # source rows), so pre-bucket source-keyed rows.
        source_ids_by_org: dict[str, set[str]] = {
            slug: set() for slug in target_orgs
        }
        for sid, slug in source_org.items():
            source_ids_by_org[slug].add(sid)

        # Tables whose FK to sources is NOT NULL — orphan rows will be
        # dropped on migration (and must be subtracted from partition
        # counts + from the verifier's expected legacy totals).
        drop_on_orphan = {"thoughts", "derivations", "note_comments",
                          "note_versions"}
        # Tables with either nullable FK (claims) or no FK (note_reads,
        # attachments) — orphan rows are kept (claims get source_id set
        # to NULL; note_reads/attachments just carry the stale id).
        for table in (
            "thoughts", "derivations", "note_comments",
            "note_versions", "note_reads", "attachments",
            "claims",
        ):
            try:
                buckets: dict[str, int] = {slug: 0 for slug in target_orgs}
                orphans = 0
                rows = conn.execute(
                    f"SELECT source_id FROM {table}"
                ).fetchall()
                for r in rows:
                    sid = r["source_id"]
                    if sid is None:
                        # claims allow NULL source_id
                        buckets[DEFAULT_FALLBACK_ORG] = (
                            buckets.get(DEFAULT_FALLBACK_ORG, 0) + 1
                        )
                        continue
                    if sid not in source_org:
                        orphans += 1
                        if table in drop_on_orphan:
                            # Row will be skipped — don't bucket.
                            continue
                        # Nullable-FK / FK-less tables keep the row and
                        # route it to the fallback org (source_org.get
                        # fallback below preserves historical behaviour).
                    home = source_org.get(sid, DEFAULT_FALLBACK_ORG)
                    buckets[home] = buckets.get(home, 0) + 1
                for slug, n in buckets.items():
                    setattr(plan.partitions[slug], table, n)
                if orphans:
                    plan.orphans_by_table[table] = orphans
            except sqlite3.OperationalError:
                # Table absent in legacy DB (older schema).
                continue

        # captures: source_id is nullable; NULL → personal (operator-local).
        # Orphan source_id → source_id set to NULL → personal (matches
        # the ON DELETE SET NULL semantics in the schema).
        try:
            buckets = {slug: 0 for slug in target_orgs}
            orphans = 0
            rows = conn.execute(
                "SELECT source_id FROM captures"
            ).fetchall()
            for r in rows:
                sid = r["source_id"]
                if sid is None:
                    buckets["personal"] = buckets.get("personal", 0) + 1
                elif sid not in source_org:
                    orphans += 1
                    buckets["personal"] = buckets.get("personal", 0) + 1
                else:
                    home = source_org.get(sid, DEFAULT_FALLBACK_ORG)
                    buckets[home] = buckets.get(home, 0) + 1
            for slug, n in buckets.items():
                plan.partitions[slug].captures = n
            if orphans:
                plan.orphans_by_table["captures"] = orphans
        except sqlite3.OperationalError:
            pass

        # entity_mentions: content_id resolves to a thought or derivation
        # source; bucket by that.
        try:
            content_to_org = _content_id_to_org(conn, source_org)
            buckets = {slug: 0 for slug in target_orgs}
            rows = conn.execute(
                "SELECT content_id FROM entity_mentions"
            ).fetchall()
            for r in rows:
                home = content_to_org.get(
                    r["content_id"], DEFAULT_FALLBACK_ORG,
                )
                buckets[home] = buckets.get(home, 0) + 1
            for slug, n in buckets.items():
                plan.partitions[slug].entity_mentions = n
        except sqlite3.OperationalError:
            pass

        # edges: bucket by source endpoint's parent source's org. Edges
        # whose endpoint isn't a source row default to autonomy.
        try:
            buckets = {slug: 0 for slug in target_orgs}
            edge_rows = conn.execute(
                "SELECT source_id, source_type FROM edges"
            ).fetchall()
            for r in edge_rows:
                home = _resolve_endpoint_org(
                    conn, r["source_id"], r["source_type"], source_org,
                )
                buckets[home] = buckets.get(home, 0) + 1
            for slug, n in buckets.items():
                plan.partitions[slug].edges = n
        except sqlite3.OperationalError:
            pass

        # entities: count distinct entities referenced by each org's
        # mentions (not raw row counts in the entities table).
        try:
            content_to_org = _content_id_to_org(conn, source_org)
            ent_buckets: dict[str, set[str]] = {
                slug: set() for slug in target_orgs
            }
            rows = conn.execute(
                "SELECT entity_id, content_id FROM entity_mentions"
            ).fetchall()
            for r in rows:
                home = content_to_org.get(
                    r["content_id"], DEFAULT_FALLBACK_ORG,
                )
                ent_buckets[home].add(r["entity_id"])
            for slug, ids in ent_buckets.items():
                plan.partitions[slug].entities = len(ids)
        except sqlite3.OperationalError:
            pass

        # tags: duplicated to every org.
        try:
            n_tags = conn.execute(
                "SELECT COUNT(*) FROM tags"
            ).fetchone()[0]
            for slug in target_orgs:
                plan.partitions[slug].tags = n_tags
        except sqlite3.OperationalError:
            pass

        # threads: operator-local — autonomy gets them all (we do not
        # have a per-thread origin signal in legacy data).
        try:
            n_threads = conn.execute(
                "SELECT COUNT(*) FROM threads"
            ).fetchone()[0]
            plan.partitions[DEFAULT_FALLBACK_ORG].threads = n_threads
        except sqlite3.OperationalError:
            pass

        # nodes / node_refs / settings: → autonomy.
        for table in ("nodes", "node_refs", "settings"):
            try:
                n = conn.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
                setattr(plan.partitions[DEFAULT_FALLBACK_ORG], table, n)
            except sqlite3.OperationalError:
                continue
    finally:
        conn.close()
    return plan


def _content_id_to_org(
    conn: sqlite3.Connection, source_org: dict[str, str],
) -> dict[str, str]:
    """Pre-build content_id → org mapping for entity_mentions partitioning."""
    mapping: dict[str, str] = {}
    for tbl in ("thoughts", "derivations"):
        try:
            for r in conn.execute(
                f"SELECT id, source_id FROM {tbl}"
            ).fetchall():
                mapping[r["id"]] = source_org.get(
                    r["source_id"], DEFAULT_FALLBACK_ORG,
                )
        except sqlite3.OperationalError:
            continue
    return mapping


def _resolve_endpoint_org(
    conn: sqlite3.Connection,
    endpoint_id: str,
    endpoint_type: str,
    source_org: dict[str, str],
) -> str:
    """Return the org slug for an edge endpoint.

    Sources resolve directly. Thoughts / derivations resolve through
    their parent source. Anything else (entities, claims, raw IDs)
    defaults to ``autonomy`` — entities are global; we duplicate where
    referenced via the entity_mentions copy path.
    """
    if endpoint_type == "source":
        return source_org.get(endpoint_id, DEFAULT_FALLBACK_ORG)
    if endpoint_type in ("thought", "derivation"):
        table = "thoughts" if endpoint_type == "thought" else "derivations"
        try:
            row = conn.execute(
                f"SELECT source_id FROM {table} WHERE id = ?",
                (endpoint_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            row = None
        if row and row["source_id"]:
            return source_org.get(row["source_id"], DEFAULT_FALLBACK_ORG)
    return DEFAULT_FALLBACK_ORG


# ── Apply ───────────────────────────────────────────────────


def _open_org_db_for_write(slug: str, orgs_dir: Path):
    """Create-or-open the per-org DB at ``orgs_dir/<slug>.db``.

    Imported lazily to avoid a circular ``db`` ↔ ``migrations`` ref at
    module load time.
    """
    from tools.graph.db import GraphDB

    path = orgs_dir / f"{slug}.db"
    if path.exists():
        return GraphDB(path)
    # Set type='shared' for everything except 'personal' (the only
    # operator-local org).
    type_ = "personal" if slug == "personal" else "shared"
    return GraphDB.create_org_db(slug, type_=type_, path=path)


def apply_migration(
    plan: MigrationPlan, *, log=print,
) -> dict[str, int]:
    """Execute the partition: insert legacy rows into each per-org DB.

    Idempotency assumed; caller invoked :func:`check_idempotency`
    before reaching here. Returns a per-org row-count summary suitable
    for printing as the post-migration report.

    The function manages its own connections — on success the legacy
    DB is left untouched (renamed by the post-migration step); on
    error the per-org DBs are left in whatever partial state they
    reached so the operator can inspect rather than silently roll back
    a half-applied migration.
    """
    plan.orgs_dir.mkdir(parents=True, exist_ok=True)
    src = _open_ro(plan.legacy_db)
    try:
        # Pre-build content_id → org for entity_mentions / edges.
        content_to_org = _content_id_to_org(src, plan.source_org)
        # Canonical set of valid source IDs — used to drop/NULL orphan
        # FK references that would otherwise trip the per-org DB's
        # PRAGMA foreign_keys = ON enforcement.
        valid_source_ids = set(plan.source_org.keys())

        org_dbs = {}
        for slug in plan.org_slugs:
            org_dbs[slug] = _open_org_db_for_write(slug, plan.orgs_dir)

        try:
            # Order matters for FK-like dependencies (sources before
            # children) and for FTS triggers to fire correctly.
            _copy_sources(src, org_dbs, plan, log=log)
            td_orphans = _copy_thoughts_and_derivations(
                src, org_dbs, plan.source_org,
                valid_source_ids=valid_source_ids, log=log,
            )
            plan.orphans_by_table.update(td_orphans)
            _copy_entities_and_mentions(
                src, org_dbs, content_to_org, log=log,
            )
            _copy_simple_source_keyed(
                src, org_dbs, plan.source_org,
                table="claims",
                cols=("id", "subject_id", "predicate", "object_id",
                      "object_val", "source_id", "asserted_by",
                      "confidence", "status", "evidence", "metadata",
                      "created_at"),
                null_target=DEFAULT_FALLBACK_ORG,
                valid_source_ids=valid_source_ids,
                fk_mode="nullable",
                log=log,
            )
            _copy_edges(src, org_dbs, plan.source_org, log=log)
            nc_orphans = _copy_simple_source_keyed(
                src, org_dbs, plan.source_org,
                table="note_comments",
                cols=("id", "source_id", "content", "actor",
                      "integrated", "created_at", "publication_state"),
                valid_source_ids=valid_source_ids,
                fk_mode="not_null",
                log=log,
            )
            if nc_orphans:
                plan.orphans_by_table["note_comments"] = nc_orphans
            nv_orphans = _copy_note_versions(
                src, org_dbs, plan.source_org,
                valid_source_ids=valid_source_ids, log=log,
            )
            if nv_orphans:
                plan.orphans_by_table["note_versions"] = nv_orphans
            _copy_simple_source_keyed(
                src, org_dbs, plan.source_org,
                table="attachments",
                cols=("id", "hash", "filename", "mime_type",
                      "size_bytes", "file_path", "source_id",
                      "turn_number", "metadata", "alt_text",
                      "created_at"),
                log=log,
            )
            _copy_note_reads(src, org_dbs, plan.source_org, log=log)
            cap_orphans = _copy_captures(
                src, org_dbs, plan.source_org,
                valid_source_ids=valid_source_ids, log=log,
            )
            if cap_orphans:
                plan.orphans_by_table["captures"] = cap_orphans
            _copy_tags_to_all(src, org_dbs, log=log)
            _copy_threads_to(src, org_dbs, DEFAULT_FALLBACK_ORG, log=log)
            _copy_global_to(
                src, org_dbs, DEFAULT_FALLBACK_ORG,
                table="nodes",
                cols=("id", "parent_id", "type", "title", "description",
                      "status", "sort_order", "metadata", "created_at",
                      "updated_at"),
                log=log,
            )
            _copy_global_to(
                src, org_dbs, DEFAULT_FALLBACK_ORG,
                table="node_refs",
                cols=("node_id", "ref_id", "ref_type", "metadata"),
                log=log,
            )
            _copy_global_to(
                src, org_dbs, DEFAULT_FALLBACK_ORG,
                table="settings",
                cols=("id", "set_id", "schema_revision", "key", "payload",
                      "publication_state", "supersedes", "excludes",
                      "deprecated", "successor_id", "created_at",
                      "updated_at"),
                log=log,
            )
            for slug, db in org_dbs.items():
                db.conn.commit()
        finally:
            for db in org_dbs.values():
                db.close()
    finally:
        src.close()

    # Final report — re-open each org DB for a fresh count so the
    # number reflects on-disk state (not the in-process partition map).
    summary: dict[str, int] = {}
    for slug in plan.org_slugs:
        path = plan.orgs_dir / f"{slug}.db"
        try:
            ro = _open_ro(path)
            try:
                summary[slug] = ro.execute(
                    "SELECT COUNT(*) FROM sources"
                ).fetchone()[0]
            finally:
                ro.close()
        except sqlite3.Error:
            summary[slug] = 0
    return summary


def _copy_sources(src, org_dbs, plan: MigrationPlan, *, log) -> None:
    rows = src.execute(
        "SELECT id, type, platform, project, title, url, file_path, "
        "metadata, created_at, ingested_at, last_activity_at, "
        "publication_state, deprecated, successor_id FROM sources"
    ).fetchall()
    inserted: dict[str, int] = {slug: 0 for slug in plan.org_slugs}
    for r in rows:
        slug = plan.source_org.get(r["id"], DEFAULT_FALLBACK_ORG)
        org_dbs[slug].conn.execute(
            "INSERT OR IGNORE INTO sources("
            "id, type, platform, project, title, url, file_path, "
            "metadata, created_at, ingested_at, last_activity_at, "
            "publication_state, deprecated, successor_id) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                r["id"], r["type"], r["platform"], r["project"],
                r["title"], r["url"], r["file_path"],
                r["metadata"], r["created_at"], r["ingested_at"],
                r["last_activity_at"], r["publication_state"],
                r["deprecated"], r["successor_id"],
            ),
        )
        inserted[slug] += 1
    log(f"  sources: {sum(inserted.values())} rows → {inserted}")


def _copy_thoughts_and_derivations(
    src, org_dbs, source_org, *, valid_source_ids, log,
) -> dict[str, int]:
    """Copy thoughts + derivations; FTS triggers fire on insert.

    Filters orphan rows (``source_id`` not in legacy ``sources``) so the
    per-org DB's NOT NULL REFERENCES sources(id) constraint isn't
    violated. For derivations, a ``thought_id`` referencing a thought
    we dropped (or that routes to a different org) is set to NULL —
    mirrors the schema's ``ON DELETE SET NULL`` semantics.
    """
    orphans: dict[str, int] = {}

    # Thoughts first so we can build a {thought_id: home_org} map that
    # derivations use to NULL out cross-org / orphaned thought_id refs.
    thought_to_org: dict[str, str] = {}
    t_cols = ("id", "source_id", "content", "role", "turn_number",
              "message_id", "tags", "metadata", "created_at",
              "publication_state")
    try:
        t_rows = src.execute(
            f"SELECT {', '.join(t_cols)} FROM thoughts"
        ).fetchall()
    except sqlite3.OperationalError:
        t_rows = None
    if t_rows is not None:
        n = 0
        skipped = 0
        for r in t_rows:
            sid = r["source_id"]
            if sid is None or sid not in valid_source_ids:
                skipped += 1
                continue
            home = source_org.get(sid, DEFAULT_FALLBACK_ORG)
            thought_to_org[r["id"]] = home
            placeholders = ", ".join("?" * len(t_cols))
            org_dbs[home].conn.execute(
                f"INSERT OR IGNORE INTO thoughts({', '.join(t_cols)}) "
                f"VALUES({placeholders})",
                tuple(r[c] for c in t_cols),
            )
            n += 1
        if skipped:
            orphans["thoughts"] = skipped
            log(f"  thoughts: {n} rows ({skipped} orphans dropped)")
        else:
            log(f"  thoughts: {n} rows")

    d_cols = ("id", "source_id", "thought_id", "content", "model",
              "turn_number", "message_id", "metadata", "created_at")
    try:
        d_rows = src.execute(
            f"SELECT {', '.join(d_cols)} FROM derivations"
        ).fetchall()
    except sqlite3.OperationalError:
        d_rows = None
    if d_rows is not None:
        n = 0
        skipped = 0
        nulled = 0
        tid_idx = d_cols.index("thought_id")
        placeholders = ", ".join("?" * len(d_cols))
        for r in d_rows:
            sid = r["source_id"]
            if sid is None or sid not in valid_source_ids:
                skipped += 1
                continue
            home = source_org.get(sid, DEFAULT_FALLBACK_ORG)
            vals = [r[c] for c in d_cols]
            tid = vals[tid_idx]
            if tid is not None and thought_to_org.get(tid) != home:
                vals[tid_idx] = None
                nulled += 1
            org_dbs[home].conn.execute(
                f"INSERT OR IGNORE INTO derivations({', '.join(d_cols)}) "
                f"VALUES({placeholders})",
                tuple(vals),
            )
            n += 1
        if skipped:
            orphans["derivations"] = skipped
        bits = []
        if skipped:
            bits.append(f"{skipped} orphans dropped")
        if nulled:
            bits.append(f"{nulled} thought_id nulled")
        suffix = f" ({', '.join(bits)})" if bits else ""
        log(f"  derivations: {n} rows{suffix}")

    return orphans


def _copy_entities_and_mentions(
    src, org_dbs, content_to_org, *, log,
) -> None:
    """Copy entity_mentions, plus the entities they reference, per-org.

    Entities are a global concept dictionary; rather than copying every
    row to every DB, we only seed entities into orgs that actually
    reference them via mentions. Orphan entities (no mentions) skip the
    migration — they were dead weight in the legacy DB anyway.
    """
    try:
        ent_rows = {
            r["id"]: r for r in src.execute(
                "SELECT id, name, canonical_name, type, description, "
                "metadata, created_at FROM entities"
            ).fetchall()
        }
    except sqlite3.OperationalError:
        ent_rows = {}

    # Plan the copy first so we don't double-insert entities.
    org_entities: dict[str, set[str]] = {}
    org_mentions: dict[str, list[tuple]] = {}
    try:
        rows = src.execute(
            "SELECT entity_id, content_id, content_type, count "
            "FROM entity_mentions"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    for r in rows:
        home = content_to_org.get(r["content_id"], DEFAULT_FALLBACK_ORG)
        org_entities.setdefault(home, set()).add(r["entity_id"])
        org_mentions.setdefault(home, []).append(
            (r["entity_id"], r["content_id"], r["content_type"], r["count"]),
        )

    n_ent = 0
    n_men = 0
    for home, eids in org_entities.items():
        for eid in eids:
            ent = ent_rows.get(eid)
            if not ent:
                continue
            org_dbs[home].conn.execute(
                "INSERT OR IGNORE INTO entities("
                "id, name, canonical_name, type, description, "
                "metadata, created_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    ent["id"], ent["name"], ent["canonical_name"],
                    ent["type"], ent["description"], ent["metadata"],
                    ent["created_at"],
                ),
            )
            n_ent += 1
    for home, mentions in org_mentions.items():
        for m in mentions:
            org_dbs[home].conn.execute(
                "INSERT OR IGNORE INTO entity_mentions("
                "entity_id, content_id, content_type, count) "
                "VALUES(?, ?, ?, ?)",
                m,
            )
            n_men += 1
    log(f"  entities: {n_ent} rows, entity_mentions: {n_men} rows")


def _copy_simple_source_keyed(
    src, org_dbs, source_org, *,
    table, cols, null_target=None,
    valid_source_ids=None, fk_mode=None, log,
) -> int:
    """Copy a source-keyed table.

    ``fk_mode``:
      * ``"not_null"``: ``source_id`` has a NOT NULL FK to sources;
        orphan rows are dropped and counted.
      * ``"nullable"``: ``source_id`` has a nullable FK with
        ``ON DELETE SET NULL``; orphan rows are kept with ``source_id``
        set to NULL and routed to ``null_target``.
      * ``None``: no FK constraint; rows are kept as-is (orphan
        ``source_id`` is harmless).

    Returns the orphan count so callers can aggregate into the migration
    summary.
    """
    try:
        rows = src.execute(
            f"SELECT {', '.join(cols)} FROM {table}"
        ).fetchall()
    except sqlite3.OperationalError:
        return 0
    placeholders = ", ".join("?" * len(cols))
    sid_idx = cols.index("source_id") if "source_id" in cols else None
    n = 0
    orphans = 0
    for r in rows:
        sid = r["source_id"]
        is_orphan = (
            sid is not None
            and valid_source_ids is not None
            and sid not in valid_source_ids
        )
        if is_orphan:
            orphans += 1
            if fk_mode == "not_null":
                continue
            if fk_mode == "nullable":
                sid = None  # fall through — routed via null_target
        if sid is None:
            home = null_target or DEFAULT_FALLBACK_ORG
        else:
            home = source_org.get(sid, DEFAULT_FALLBACK_ORG)
        vals = [r[c] for c in cols]
        if is_orphan and fk_mode == "nullable" and sid_idx is not None:
            vals[sid_idx] = None
        org_dbs[home].conn.execute(
            f"INSERT OR IGNORE INTO {table}({', '.join(cols)}) "
            f"VALUES({placeholders})",
            tuple(vals),
        )
        n += 1
    if orphans:
        action = "dropped" if fk_mode == "not_null" else "nulled"
        log(f"  {table}: {n} rows ({orphans} orphans {action})")
    else:
        log(f"  {table}: {n} rows")
    return orphans


def _copy_note_versions(
    src, org_dbs, source_org, *, valid_source_ids=None, log,
) -> int:
    """note_versions has an AUTOINCREMENT id we leave SQLite to mint.

    ``source_id`` is NOT NULL REFERENCES sources — orphan rows are
    dropped to avoid the FK violation.
    """
    try:
        rows = src.execute(
            "SELECT source_id, version, content, created_at "
            "FROM note_versions"
        ).fetchall()
    except sqlite3.OperationalError:
        return 0
    n = 0
    orphans = 0
    for r in rows:
        sid = r["source_id"]
        if sid is None or (
            valid_source_ids is not None and sid not in valid_source_ids
        ):
            orphans += 1
            continue
        home = source_org.get(sid, DEFAULT_FALLBACK_ORG)
        org_dbs[home].conn.execute(
            "INSERT OR IGNORE INTO note_versions("
            "source_id, version, content, created_at) "
            "VALUES(?, ?, ?, ?)",
            (sid, r["version"], r["content"], r["created_at"]),
        )
        n += 1
    if orphans:
        log(f"  note_versions: {n} rows ({orphans} orphans dropped)")
    else:
        log(f"  note_versions: {n} rows")
    return orphans


def _copy_note_reads(src, org_dbs, source_org, *, log) -> None:
    try:
        rows = src.execute(
            "SELECT source_id, actor, ts FROM note_reads"
        ).fetchall()
    except sqlite3.OperationalError:
        return
    n = 0
    for r in rows:
        home = source_org.get(r["source_id"], DEFAULT_FALLBACK_ORG)
        org_dbs[home].conn.execute(
            "INSERT OR IGNORE INTO note_reads(source_id, actor, ts) "
            "VALUES(?, ?, ?)",
            (r["source_id"], r["actor"], r["ts"]),
        )
        n += 1
    log(f"  note_reads: {n} rows")


def _copy_captures(
    src, org_dbs, source_org, *,
    valid_source_ids=None, thread_home=DEFAULT_FALLBACK_ORG, log,
) -> int:
    """Copy captures.

    ``captures.source_id`` and ``captures.thread_id`` both have
    ``ON DELETE SET NULL`` FKs. Threads live only in ``thread_home``
    (``autonomy`` by default), so any capture landing elsewhere has
    its ``thread_id`` nulled. Orphan ``source_id`` is likewise nulled,
    which re-routes the capture to ``personal``.
    """
    try:
        rows = src.execute(
            "SELECT id, content, thread_id, source_id, turn_number, "
            "status, actor, metadata, created_at, publication_state "
            "FROM captures"
        ).fetchall()
    except sqlite3.OperationalError:
        return 0
    n = 0
    orphans = 0
    for r in rows:
        sid = r["source_id"]
        if sid is not None and (
            valid_source_ids is not None and sid not in valid_source_ids
        ):
            orphans += 1
            sid = None
        if sid is None:
            home = "personal"
        else:
            home = source_org.get(sid, DEFAULT_FALLBACK_ORG)
        tid = r["thread_id"]
        if tid is not None and home != thread_home:
            tid = None
        org_dbs[home].conn.execute(
            "INSERT OR IGNORE INTO captures("
            "id, content, thread_id, source_id, turn_number, "
            "status, actor, metadata, created_at, publication_state) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                r["id"], r["content"], tid, sid,
                r["turn_number"], r["status"], r["actor"], r["metadata"],
                r["created_at"], r["publication_state"],
            ),
        )
        n += 1
    if orphans:
        log(f"  captures: {n} rows ({orphans} orphans nulled → personal)")
    else:
        log(f"  captures: {n} rows")
    return orphans


def _copy_edges(src, org_dbs, source_org, *, log) -> None:
    """Edges follow their source endpoint. Cross-org edges land in the
    org of the source endpoint only — when cross-org read enablement
    (auto-txg5.4) lands, the cross-org resolver will follow them.
    """
    try:
        rows = src.execute(
            "SELECT id, source_id, source_type, target_id, target_type, "
            "relation, weight, metadata, created_at FROM edges"
        ).fetchall()
    except sqlite3.OperationalError:
        return
    n = 0
    for r in rows:
        home = _resolve_endpoint_org(
            src, r["source_id"], r["source_type"], source_org,
        )
        org_dbs[home].conn.execute(
            "INSERT OR IGNORE INTO edges("
            "id, source_id, source_type, target_id, target_type, "
            "relation, weight, metadata, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                r["id"], r["source_id"], r["source_type"],
                r["target_id"], r["target_type"], r["relation"],
                r["weight"], r["metadata"], r["created_at"],
            ),
        )
        n += 1
    log(f"  edges: {n} rows")


def _copy_tags_to_all(src, org_dbs, *, log) -> None:
    """Tags are a global vocabulary — every org gets the full table."""
    try:
        rows = src.execute(
            "SELECT name, description, created_by, created_at, updated_at "
            "FROM tags"
        ).fetchall()
    except sqlite3.OperationalError:
        return
    for r in rows:
        for db in org_dbs.values():
            db.conn.execute(
                "INSERT OR IGNORE INTO tags("
                "name, description, created_by, created_at, updated_at) "
                "VALUES(?, ?, ?, ?, ?)",
                (r["name"], r["description"], r["created_by"],
                 r["created_at"], r["updated_at"]),
            )
    log(f"  tags: {len(rows)} rows × {len(org_dbs)} orgs")


def _copy_threads_to(src, org_dbs, target: str, *, log) -> None:
    try:
        rows = src.execute(
            "SELECT id, title, status, priority, summary, created_by, "
            "metadata, created_at, updated_at FROM threads"
        ).fetchall()
    except sqlite3.OperationalError:
        return
    if target not in org_dbs:
        return
    for r in rows:
        org_dbs[target].conn.execute(
            "INSERT OR IGNORE INTO threads("
            "id, title, status, priority, summary, created_by, "
            "metadata, created_at, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (r["id"], r["title"], r["status"], r["priority"], r["summary"],
             r["created_by"], r["metadata"], r["created_at"],
             r["updated_at"]),
        )
    log(f"  threads: {len(rows)} rows → {target}")


def _copy_global_to(src, org_dbs, target: str, *, table, cols, log) -> None:
    if target not in org_dbs:
        return
    try:
        rows = src.execute(
            f"SELECT {', '.join(cols)} FROM {table}"
        ).fetchall()
    except sqlite3.OperationalError:
        return
    placeholders = ", ".join("?" * len(cols))
    for r in rows:
        org_dbs[target].conn.execute(
            f"INSERT OR IGNORE INTO {table}({', '.join(cols)}) "
            f"VALUES({placeholders})",
            tuple(r[c] for c in cols),
        )
    log(f"  {table}: {len(rows)} rows → {target}")


# ── Verification ────────────────────────────────────────────


@dataclass
class VerificationReport:
    ok: bool
    issues: list[str] = field(default_factory=list)
    counts: dict[str, dict[str, int]] = field(default_factory=dict)


def verify_migration(plan: MigrationPlan) -> VerificationReport:
    """Cross-check legacy row counts against the per-org DBs.

    For every per-source table we sum the per-org counts and compare to
    the legacy total. FTS5 indices are checked by ensuring
    ``COUNT(sources_fts)`` would match ``COUNT(sources)`` if the FTS5
    auxiliary table existed (it doesn't for sources today — the FTS
    indices live on ``thoughts_fts`` and ``derivations_fts``, populated
    via triggers). We assert FTS rows match thought/derivation rows.
    """
    report = VerificationReport(ok=True)

    src = _open_ro(plan.legacy_db)
    legacy_counts: dict[str, int] = {}
    try:
        for tbl in (
            "sources", "thoughts", "derivations", "entity_mentions",
            "edges", "claims", "note_comments", "note_versions",
            "note_reads", "attachments", "captures",
        ):
            try:
                legacy_counts[tbl] = src.execute(
                    f"SELECT COUNT(*) FROM {tbl}"
                ).fetchone()[0]
            except sqlite3.OperationalError:
                legacy_counts[tbl] = 0
    finally:
        src.close()

    org_counts: dict[str, dict[str, int]] = {}
    for slug in plan.org_slugs:
        path = plan.orgs_dir / f"{slug}.db"
        if not path.exists():
            report.ok = False
            report.issues.append(f"missing per-org DB: {path}")
            continue
        org_counts[slug] = {}
        try:
            ro = _open_ro(path)
        except sqlite3.Error as e:
            report.ok = False
            report.issues.append(f"cannot open {path}: {e}")
            continue
        try:
            for tbl in legacy_counts:
                try:
                    org_counts[slug][tbl] = ro.execute(
                        f"SELECT COUNT(*) FROM {tbl}"
                    ).fetchone()[0]
                except sqlite3.OperationalError:
                    org_counts[slug][tbl] = 0
            # FTS5 sanity: thoughts_fts row count == thoughts row count.
            for fts, base in (
                ("thoughts_fts", "thoughts"),
                ("derivations_fts", "derivations"),
            ):
                try:
                    n_fts = ro.execute(
                        f"SELECT COUNT(*) FROM {fts}"
                    ).fetchone()[0]
                    n_base = ro.execute(
                        f"SELECT COUNT(*) FROM {base}"
                    ).fetchone()[0]
                    if n_fts != n_base:
                        report.ok = False
                        report.issues.append(
                            f"{slug}: {fts} rows ({n_fts}) != "
                            f"{base} rows ({n_base})"
                        )
                except sqlite3.OperationalError as e:
                    report.issues.append(
                        f"{slug}: cannot count {fts}: {e}"
                    )
        finally:
            ro.close()

    # Orphans dropped during migration shrink the expected per-org sum;
    # orphans whose source_id was nulled (claims, captures) stay counted.
    _dropped_on_orphan = {"thoughts", "derivations",
                          "note_comments", "note_versions"}

    # Sum per-table row counts across orgs and compare.
    for tbl, expected in legacy_counts.items():
        total = sum(c.get(tbl, 0) for c in org_counts.values())
        if tbl == "tags":
            # Tags are duplicated across orgs; not a sum check.
            continue
        adjusted = expected
        if tbl in _dropped_on_orphan:
            adjusted -= plan.orphans_by_table.get(tbl, 0)
        if total != adjusted:
            report.ok = False
            orphan_note = ""
            if tbl in _dropped_on_orphan and plan.orphans_by_table.get(tbl):
                orphan_note = (
                    f" (expected adjusted for "
                    f"{plan.orphans_by_table[tbl]} orphan rows)"
                )
            report.issues.append(
                f"{tbl}: legacy={expected}, per-org sum={total}"
                f"{orphan_note}"
            )

    report.counts = org_counts
    return report


# ── Post-migration ──────────────────────────────────────────


def rename_legacy(legacy_db: Path, *, timestamp: str) -> Path:
    """Move ``data/graph.db`` to ``data/graph.db.legacy-<ts>``.

    Operator manually deletes the renamed file later (per bead spec —
    keep the backup until production stability is confirmed).
    """
    new_path = legacy_db.parent / f"{legacy_db.name}.legacy-{timestamp}"
    legacy_db.rename(new_path)
    for suffix in ("-wal", "-shm"):
        side = legacy_db.parent / f"{legacy_db.name}{suffix}"
        if side.exists():
            side.rename(legacy_db.parent / f"{new_path.name}{suffix}")
    return new_path


# ── CLI ─────────────────────────────────────────────────────


def _print_plan(plan: MigrationPlan) -> None:
    print("Migration plan")
    print(f"  Legacy DB: {plan.legacy_db}")
    print(f"  Per-org dir: {plan.orgs_dir}")
    print(f"  Target orgs: {', '.join(plan.org_slugs)}")
    if plan.null_project_count:
        print(
            f"  NULL project rows: {plan.null_project_count} "
            f"→ {DEFAULT_FALLBACK_ORG}"
        )
    if plan.unknown_projects:
        for proj, n in sorted(plan.unknown_projects.items()):
            print(
                f"  Unknown project {proj!r}: {n} rows "
                f"→ {DEFAULT_FALLBACK_ORG} (warning)"
            )
    if plan.orphans_by_table:
        # Tables whose orphans are dropped (NOT NULL FK to sources) vs.
        # kept with source_id nulled (nullable FK) — the distinction
        # matters for the operator's expectations about row counts.
        _drop_tables = {"thoughts", "derivations",
                        "note_comments", "note_versions"}
        for tbl, n in sorted(plan.orphans_by_table.items()):
            action = "dropped" if tbl in _drop_tables else "source_id → NULL"
            print(f"  Orphan {tbl}: {n} rows ({action})")
    print()
    print(
        f"{'org':<20} {'src':>6} {'thg':>6} {'der':>6} {'edg':>6} "
        f"{'cmt':>5} {'att':>5} {'cap':>5} {'tag':>5}"
    )
    for slug in plan.org_slugs:
        p = plan.partitions[slug]
        print(
            f"{slug:<20} {p.sources:>6} {p.thoughts:>6} {p.derivations:>6} "
            f"{p.edges:>6} {p.note_comments:>5} {p.attachments:>5} "
            f"{p.captures:>5} {p.tags:>5}"
        )


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Split data/graph.db into per-org DBs (auto-9iq2s).",
    )
    ap.add_argument(
        "--legacy-db", type=Path, default=DEFAULT_LEGACY_DB,
        help=f"Path to legacy graph.db (default: {DEFAULT_LEGACY_DB})",
    )
    ap.add_argument(
        "--orgs-dir", type=Path, default=None,
        help="Per-org DB root (default: AUTONOMY_ORGS_DIR or data/orgs)",
    )
    ap.add_argument(
        "--projects-yaml", type=Path, default=DEFAULT_PROJECTS_YAML,
        help="Path to agents/projects.yaml for org enumeration",
    )
    ap.add_argument(
        "--pid-dir", type=Path, default=DEFAULT_PID_DIR,
        help="Where to look for dashboard.pid / dispatcher.pid",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Print the partition plan and exit without touching DBs",
    )
    ap.add_argument(
        "--no-backup", action="store_true",
        help="(test only) skip the pre-flight legacy DB backup",
    )
    ap.add_argument(
        "--no-rename", action="store_true",
        help="Skip renaming legacy graph.db at the end (test mode)",
    )
    args = ap.parse_args(argv)

    orgs_dir = args.orgs_dir
    if orgs_dir is None:
        env = os.environ.get("AUTONOMY_ORGS_DIR")
        orgs_dir = Path(env) if env else DEFAULT_ORGS_DIR

    try:
        if not args.dry_run:
            check_idempotency(orgs_dir, args.legacy_db)
            check_services_stopped(args.pid_dir)
        plan = build_plan(args.legacy_db, orgs_dir, args.projects_yaml)
        _print_plan(plan)
        if args.dry_run:
            return 0
        timestamp = _ts()
        if not args.no_backup:
            backups = backup_legacy_db(args.legacy_db, timestamp=timestamp)
            print(f"Backup written: {[str(b) for b in backups]}")
        print()
        print("Applying migration:")
        summary = apply_migration(plan, log=print)
        print()
        print(f"Per-org source row counts: {summary}")
        report = verify_migration(plan)
        if not report.ok:
            print("Verification FAILED:", file=sys.stderr)
            for issue in report.issues:
                print(f"  {issue}", file=sys.stderr)
            return 2
        print("Verification OK")
        if not args.no_rename:
            renamed = rename_legacy(args.legacy_db, timestamp=timestamp)
            print(f"Legacy DB renamed: {renamed}")
        return 0
    except AlreadyMigratedError as e:
        print(f"ALREADY MIGRATED: {e}", file=sys.stderr)
        return 3
    except PreflightError as e:
        print(f"PREFLIGHT FAILED: {e}", file=sys.stderr)
        return 4
    except MigrationError as e:
        print(f"MIGRATION FAILED: {e}", file=sys.stderr)
        return 5


if __name__ == "__main__":
    sys.exit(main())
