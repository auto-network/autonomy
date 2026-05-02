"""Cache GC sweep — delete expired ``@cache`` Setting rows.

Spec: bead auto-5ch66 (graph://269bb2b4-caa). The ``@cache(ttl=...)``
decorator stamps an absolute ``expires_at`` on every write to a
cache-tagged schema; this module sweeps rows whose ``expires_at`` has
elapsed and whose ``publication_state`` is below ``published`` (cache
rows should never reach the federated states; the state guard is
defense in depth).

The sweep is per-org (one DB at a time) and bounded by ``--limit`` (1000
default per DB per pass). The CLI verb ``graph maintenance cache-gc``
calls into :func:`run_cache_gc` here; an external cron invokes the CLI
hourly. ``--dry-run`` skips DELETEs while still emitting per-row +
summary log lines.

Structured logs land on the ``graph.cache_gc`` logger — the host wires
the destination (file, journald, stdout collector) at startup; the
sweep itself doesn't pick a sink.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..db import GraphDB


logger = logging.getLogger("graph.cache_gc")


DEFAULT_LIMIT = 1000


# ── Result types ─────────────────────────────────────────────


@dataclass
class OrgSweepResult:
    """Outcome of sweeping one DB.

    ``swept`` counts deleted rows (or, in dry-run, rows that WOULD be
    deleted). ``by_set`` breaks the count down by ``set_id``.
    ``skipped_published`` counts elapsed cache rows that the state guard
    spared — should always be zero in healthy operation; non-zero is a
    signal that a cache row reached a federated publication state.
    """
    org: str
    swept: int = 0
    by_set: dict[str, int] = field(default_factory=dict)
    skipped_published: int = 0


@dataclass
class CacheGCReport:
    """Aggregate result across every DB the sweep touched."""
    swept: int = 0
    skipped_published: int = 0
    by_org: dict[str, OrgSweepResult] = field(default_factory=dict)


# ── Core sweep ───────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sweep_db(
    db: GraphDB,
    *,
    org: str,
    now_iso: str | None = None,
    limit: int = DEFAULT_LIMIT,
    dry_run: bool = False,
) -> OrgSweepResult:
    """Sweep one DB. Returns counts; emits per-row + summary log lines.

    Pure function on ``db`` — does not open or close it. Callers that
    use pooled handles (``cross_org.open_peer_db``) MUST NOT close them
    between calls.
    """
    now = now_iso or _now_iso()
    result = OrgSweepResult(org=org)

    # Defense-in-depth WARN: surface elapsed cache rows that have been
    # promoted past the GC's reach. Counting only — they are not
    # deleted (federation-load-bearing) and we want the upstream bug
    # to surface, not the row to silently vanish.
    skipped = db.conn.execute(
        "SELECT id, set_id, key, expires_at, publication_state "
        "FROM settings "
        "WHERE expires_at IS NOT NULL "
        "  AND expires_at < ? "
        "  AND publication_state IN ('published', 'canonical')",
        (now,),
    ).fetchall()
    for row in skipped:
        result.skipped_published += 1
        logger.warning(
            "published cache row skipped — cache rows should not reach "
            "published state: %s",
            json.dumps({
                "ts": now,
                "org": org,
                "set_id": row["set_id"],
                "key": row["key"],
                "expires_at": row["expires_at"],
                "publication_state": row["publication_state"],
                "reason": "cache_row_promoted",
            }, sort_keys=True),
        )

    rows = db.conn.execute(
        "SELECT id, set_id, key, expires_at, publication_state "
        "FROM settings "
        "WHERE expires_at IS NOT NULL "
        "  AND expires_at < ? "
        "  AND publication_state IN ('raw', 'curated') "
        "LIMIT ?",
        (now, int(limit)),
    ).fetchall()

    for row in rows:
        record = {
            "ts": now,
            "set_id": row["set_id"],
            "key": row["key"],
            "org": org,
            "expires_at": row["expires_at"],
            "publication_state": row["publication_state"],
            "reason": "ttl_expired",
        }
        if dry_run:
            record["dry_run"] = True
        logger.info("ttl_expired %s", json.dumps(record, sort_keys=True))
        if not dry_run:
            db.conn.execute(
                "DELETE FROM settings WHERE id = ?", (row["id"],),
            )
        result.swept += 1
        result.by_set[row["set_id"]] = result.by_set.get(row["set_id"], 0) + 1

    if not dry_run:
        db.conn.commit()

    summary = {
        "ts": now,
        "org": org,
        "swept": result.swept,
        "by_set": result.by_set,
        "skipped_published": result.skipped_published,
    }
    if dry_run:
        summary["dry_run"] = True
    logger.info("sweep_summary %s", json.dumps(summary, sort_keys=True))
    return result


# ── CLI entry ────────────────────────────────────────────────


def run_cache_gc(
    *,
    org: str | None = None,
    limit: int = DEFAULT_LIMIT,
    dry_run: bool = False,
) -> CacheGCReport:
    """Sweep every per-org DB (or one when ``org`` is given).

    Opens each org's DB read-write via :meth:`GraphDB.for_org` (the
    pool's ``rw`` slot, distinct from the read-only slot used by
    cross-org reads). Handles MUST NOT be closed by callers — the pool
    retains them; ``GraphDB.close_all_pooled`` is owned by
    startup/teardown.
    """
    from ..cross_org import list_org_slugs

    report = CacheGCReport()
    now = _now_iso()

    if org is not None:
        if org not in list_org_slugs():
            raise ValueError(f"unknown org: {org!r}")
        slugs = [org]
    else:
        slugs = sorted(list_org_slugs())

    for slug in slugs:
        try:
            db = GraphDB.for_org(slug, mode="rw")
        except FileNotFoundError:
            continue
        res = sweep_db(
            db,
            org=slug,
            now_iso=now,
            limit=limit,
            dry_run=dry_run,
        )
        report.by_org[slug] = res
        report.swept += res.swept
        report.skipped_published += res.skipped_published

    return report


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="graph maintenance cache-gc",
        description=(
            "Sweep expired ``@cache(ttl=...)`` Setting rows. Honours "
            "publication_state (raw/curated only); published/canonical "
            "rows are spared with a WARNING log."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Emit logs but do not DELETE rows.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=(
            f"Maximum rows to sweep per DB per pass (default: {DEFAULT_LIMIT}). "
            "Keeps a single sweep bounded so it is safe to cron at "
            "high frequency."
        ),
    )
    parser.add_argument(
        "--org",
        default=None,
        help="Restrict to one org slug. Default: every per-org DB.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns process exit code."""
    parser = _build_argparser()
    args = parser.parse_args(argv)

    # Default formatter so structured records reach stderr in test /
    # ad-hoc runs. Production hosts wire their own handlers via
    # ``logging.config`` and these basicConfig calls become no-ops.
    if not logger.handlers and not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, stream=sys.stderr)

    report = run_cache_gc(
        org=args.org,
        limit=args.limit,
        dry_run=args.dry_run,
    )

    print(json.dumps({
        "swept": report.swept,
        "skipped_published": report.skipped_published,
        "by_org": {
            slug: {"swept": r.swept, "by_set": r.by_set}
            for slug, r in report.by_org.items()
        },
        "dry_run": bool(args.dry_run),
    }, sort_keys=True))
    return 0


def cmd_cache_gc(args: Any) -> None:
    """``graph maintenance cache-gc`` argparse callback.

    The CLI parser at :mod:`tools.graph.cli` constructs this callback
    via ``set_defaults(func=...)``; we forward to :func:`run_cache_gc`
    using the parsed argparse namespace.
    """
    if not logger.handlers and not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    report = run_cache_gc(
        org=getattr(args, "org", None),
        limit=getattr(args, "limit", DEFAULT_LIMIT),
        dry_run=getattr(args, "dry_run", False),
    )
    print(json.dumps({
        "swept": report.swept,
        "skipped_published": report.skipped_published,
        "by_org": {
            slug: {"swept": r.swept, "by_set": r.by_set}
            for slug, r in report.by_org.items()
        },
        "dry_run": bool(getattr(args, "dry_run", False)),
    }, sort_keys=True))


if __name__ == "__main__":
    sys.exit(main())
