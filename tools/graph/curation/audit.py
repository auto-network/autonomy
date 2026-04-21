"""Audit pass for the bootstrap public-surface curation flow.

Enumerates notes that are candidates for promotion — notes tagged
``signpost``, ``architecture``, or ``protocol``, plus anything named in the
committed allowlist — and reports current tags, last-updated timestamp,
pending-comments count, and proposed state. Operator reads the report,
updates the allowlist, and runs :mod:`tools.graph.curation.promote`.

CLI::

    python -m tools.graph.curation.audit \\
        --allowlist tools/graph/curation/autonomy-bootstrap-allowlist.yaml \\
        --output /tmp/autonomy-audit.txt

``--db`` (or ``GRAPH_DB`` env var) selects the org DB to audit against.
Default is the one selected by ``ops._db_path()``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from tools.graph.db import GraphDB

from .allowlist import Allowlist, AllowlistEntry, DEFAULT_AUTONOMY_PATH, load as load_allowlist


AUDIT_TAG_SEEDS = ("signpost", "architecture", "protocol")


@dataclass
class AuditRow:
    source_id: str
    title: str | None
    tags: list[str]
    current_state: str
    proposed_state: str
    last_updated: str
    pending_comments: int
    allowlist_tier: str | None   # "canonical", "published", or None (seed-tag match only)
    ambiguous_prefix: bool = False
    missing: bool = False
    candidate_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "title": self.title,
            "tags": list(self.tags),
            "current_state": self.current_state,
            "proposed_state": self.proposed_state,
            "last_updated": self.last_updated,
            "pending_comments": self.pending_comments,
            "allowlist_tier": self.allowlist_tier,
            "ambiguous_prefix": self.ambiguous_prefix,
            "missing": self.missing,
            "candidate_ids": list(self.candidate_ids),
        }


@dataclass
class AuditReport:
    db_path: str
    allowlist_path: str
    org: str
    rows: list[AuditRow] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "db_path": self.db_path,
            "allowlist_path": self.allowlist_path,
            "org": self.org,
            "rows": [r.to_dict() for r in self.rows],
        }


# ── Core logic ───────────────────────────────────────────────


def _source_tags(row: dict) -> list[str]:
    meta = row.get("metadata")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except json.JSONDecodeError:
            return []
    if not isinstance(meta, dict):
        return []
    tags = meta.get("tags", []) or []
    return [t for t in tags if isinstance(t, str)]


def _last_updated(db: GraphDB, source_id: str, fallback: str) -> str:
    row = db.conn.execute(
        "SELECT MAX(created_at) AS ts FROM note_versions WHERE source_id = ?",
        (source_id,),
    ).fetchone()
    if row and row["ts"]:
        return row["ts"]
    return fallback


def _pending_comments(db: GraphDB, source_id: str) -> int:
    row = db.conn.execute(
        "SELECT COUNT(*) AS n FROM note_comments "
        "WHERE source_id = ? AND integrated = 0",
        (source_id,),
    ).fetchone()
    return int(row["n"] or 0)


def _resolve_prefix(db: GraphDB, prefix: str) -> list[dict]:
    """Return all sources whose id starts with ``prefix``. Empty on miss."""
    rows = db.conn.execute(
        "SELECT * FROM sources WHERE id = ? OR id LIKE ?",
        (prefix, f"{prefix}%"),
    ).fetchall()
    return [dict(r) for r in rows]


def _seed_tag_sources(db: GraphDB) -> list[dict]:
    collected: dict[str, dict] = {}
    for tag in AUDIT_TAG_SEEDS:
        for row in db.sources_with_tag(tag):
            collected[row["id"]] = row
    return list(collected.values())


def build_report(
    *,
    db: GraphDB,
    allowlist: Allowlist,
    db_path: str,
) -> AuditReport:
    report = AuditReport(db_path=db_path, allowlist_path=str(allowlist.path), org=allowlist.org)
    seen: set[str] = set()

    # 1. Every explicit allowlist entry gets a row — even if unresolved, so
    # the operator can see the gap before the runner trips on it.
    for entry in allowlist.tiers():
        matches = _resolve_prefix(db, entry.prefix)
        if not matches:
            report.rows.append(
                AuditRow(
                    source_id=entry.prefix,
                    title=None,
                    tags=[],
                    current_state="-",
                    proposed_state=entry.target_state,
                    last_updated="-",
                    pending_comments=0,
                    allowlist_tier=entry.target_state,
                    missing=True,
                )
            )
            continue
        if len(matches) > 1:
            report.rows.append(
                AuditRow(
                    source_id=entry.prefix,
                    title=None,
                    tags=[],
                    current_state="-",
                    proposed_state=entry.target_state,
                    last_updated="-",
                    pending_comments=0,
                    allowlist_tier=entry.target_state,
                    ambiguous_prefix=True,
                    candidate_ids=[m["id"] for m in matches],
                )
            )
            continue
        row = matches[0]
        report.rows.append(_row_from_source(db, row, entry.target_state, entry.target_state))
        seen.add(row["id"])

    # 2. Seed-tag candidates not already covered by the allowlist. Their
    # ``proposed_state`` is reported as the current state — the operator
    # decides whether to promote by editing the allowlist.
    for row in _seed_tag_sources(db):
        if row["id"] in seen:
            continue
        report.rows.append(
            _row_from_source(db, row, row["publication_state"], allowlist_tier=None)
        )
        seen.add(row["id"])

    report.rows.sort(key=_sort_key)
    return report


def _row_from_source(
    db: GraphDB,
    row: dict,
    proposed_state: str,
    allowlist_tier: str | None,
) -> AuditRow:
    tags = _source_tags(row)
    return AuditRow(
        source_id=row["id"],
        title=row.get("title"),
        tags=tags,
        current_state=row["publication_state"],
        proposed_state=proposed_state,
        last_updated=_last_updated(db, row["id"], row.get("ingested_at") or "-"),
        pending_comments=_pending_comments(db, row["id"]),
        allowlist_tier=allowlist_tier,
    )


_TIER_ORDER = {"canonical": 0, "published": 1, None: 2}


def _sort_key(row: AuditRow) -> tuple:
    return (_TIER_ORDER.get(row.allowlist_tier, 3), (row.title or "").lower(), row.source_id)


# ── Rendering ────────────────────────────────────────────────


def render_text(report: AuditReport) -> str:
    lines: list[str] = []
    lines.append(f"# Bootstrap allowlist audit — org={report.org}")
    lines.append(f"# db={report.db_path}")
    lines.append(f"# allowlist={report.allowlist_path}")
    lines.append("")
    canonical_rows = [r for r in report.rows if r.allowlist_tier == "canonical"]
    published_rows = [r for r in report.rows if r.allowlist_tier == "published"]
    seed_rows = [r for r in report.rows if r.allowlist_tier is None]
    for title, rows in (
        ("canonical candidates", canonical_rows),
        ("published candidates", published_rows),
        ("seed-tag candidates (not in allowlist)", seed_rows),
    ):
        lines.append(f"## {title} ({len(rows)})")
        lines.append("")
        if not rows:
            lines.append("  (none)")
            lines.append("")
            continue
        for r in rows:
            lines.append(_render_row(r))
        lines.append("")

    total_missing = sum(1 for r in report.rows if r.missing)
    total_ambiguous = sum(1 for r in report.rows if r.ambiguous_prefix)
    total_blocked = sum(
        1 for r in report.rows
        if r.allowlist_tier == "canonical" and r.pending_comments > 0
    )
    lines.append("## summary")
    lines.append(f"  rows: {len(report.rows)}")
    lines.append(f"  missing: {total_missing}")
    lines.append(f"  ambiguous: {total_ambiguous}")
    lines.append(
        f"  canonical candidates with pending comments (must integrate first): {total_blocked}"
    )
    return "\n".join(lines) + "\n"


def _render_row(r: AuditRow) -> str:
    bits: list[str] = []
    bits.append(f"  {r.source_id[:12]:<12}")
    if r.missing:
        bits.append("  [MISSING] not found in db")
    elif r.ambiguous_prefix:
        bits.append(
            f"  [AMBIGUOUS] prefix matches {len(r.candidate_ids)}: "
            + ", ".join(c[:12] for c in r.candidate_ids[:4])
        )
    else:
        state = f"{r.current_state}→{r.proposed_state}" if r.current_state != r.proposed_state else r.current_state
        tags = ",".join(r.tags) if r.tags else "-"
        bits.append(f"  state={state}  tags=[{tags}]  last_updated={r.last_updated}")
        bits.append(f"  pending_comments={r.pending_comments}")
        if r.title:
            bits.append(f"  title={r.title}")
    return "  ".join(bits)


# ── CLI ──────────────────────────────────────────────────────


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tools.graph.curation.audit",
        description=(
            "Audit pass for the bootstrap public-surface curation flow "
            "(charter: graph://93cf3026-1df)."
        ),
    )
    p.add_argument(
        "--allowlist", default=str(DEFAULT_AUTONOMY_PATH),
        help="Allowlist YAML (default: packaged autonomy list).",
    )
    p.add_argument(
        "--db", default=None,
        help="Graph DB path. Defaults to GRAPH_DB env or ops routing.",
    )
    p.add_argument(
        "--output", "-o", default=None,
        help="Write text report to this path (default: stdout).",
    )
    p.add_argument(
        "--json", action="store_true",
        help="Emit a JSON report instead of the human-readable text form.",
    )
    return p


def _resolve_db_path(cli_db: str | None) -> str:
    if cli_db:
        return cli_db
    env = os.environ.get("GRAPH_DB")
    if env:
        return env
    # Fall through to GraphDB's default resolution.
    from tools.graph.ops import _db_path  # local import to avoid cycles
    path = _db_path(None)
    if not path:
        raise SystemExit("no GRAPH_DB configured and no --db given")
    return path


def run(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    allowlist = load_allowlist(args.allowlist)
    db_path = _resolve_db_path(args.db)
    db = GraphDB(db_path)
    try:
        report = build_report(db=db, allowlist=allowlist, db_path=db_path)
    finally:
        db.close()
    output = json.dumps(report.to_dict(), indent=2) if args.json else render_text(report)
    if args.output:
        Path(args.output).write_text(output)
    else:
        sys.stdout.write(output)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run())
