"""Bulk promotion runner for the bootstrap public-surface curation flow.

Reads a committed allowlist, validates each entry against the target DB, and
applies the publication_state transitions through :func:`ops.promote_source`.
On success, files a canonical audit note that captures the session's
provenance: timestamp, actor, and the list of transitions.

Refuses to promote a ``canonical`` entry that still has unintegrated
comments — the Note Revision Protocol (graph://843a8137-3c7) says a
canonical note with stale comments is incorrect by construction. Run
``graph note update <id> --integrate ...`` first, then retry.

CLI::

    python -m tools.graph.curation.promote \\
        --allowlist tools/graph/curation/autonomy-bootstrap-allowlist.yaml \\
        --caller-org autonomy

``--dry-run`` prints the plan without touching the DB. Default behavior is to
refuse to write when any entry is missing, ambiguous, or has pending comments.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from tools.graph import ops
from tools.graph.db import GraphDB
from tools.graph.models import Source, Thought, new_id, now_iso

from .allowlist import Allowlist, AllowlistEntry, DEFAULT_AUTONOMY_PATH, load as load_allowlist


# ── Planning ──────────────────────────────────────────────────


@dataclass
class PlanEntry:
    prefix: str
    target_state: str
    resolved_id: str | None = None
    prev_state: str | None = None
    pending_comments: int = 0
    status: str = "ok"     # ok | missing | ambiguous | blocked-comments | already-at-target
    candidates: list[str] = field(default_factory=list)

    def is_actionable(self) -> bool:
        return self.status == "ok"


@dataclass
class Plan:
    org: str
    allowlist_path: str
    entries: list[PlanEntry] = field(default_factory=list)

    def blockers(self) -> list[PlanEntry]:
        return [e for e in self.entries if e.status in ("missing", "ambiguous", "blocked-comments")]

    def actionable(self) -> list[PlanEntry]:
        return [e for e in self.entries if e.is_actionable()]


def _resolve_prefix(db: GraphDB, prefix: str) -> list[dict]:
    rows = db.conn.execute(
        "SELECT * FROM sources WHERE id = ? OR id LIKE ?",
        (prefix, f"{prefix}%"),
    ).fetchall()
    return [dict(r) for r in rows]


def _pending_comments(db: GraphDB, source_id: str) -> int:
    row = db.conn.execute(
        "SELECT COUNT(*) AS n FROM note_comments "
        "WHERE source_id = ? AND integrated = 0",
        (source_id,),
    ).fetchone()
    return int(row["n"] or 0)


def build_plan(*, db: GraphDB, allowlist: Allowlist) -> Plan:
    plan = Plan(org=allowlist.org, allowlist_path=str(allowlist.path))
    for entry in allowlist.tiers():
        pe = PlanEntry(prefix=entry.prefix, target_state=entry.target_state)
        matches = _resolve_prefix(db, entry.prefix)
        if not matches:
            pe.status = "missing"
            plan.entries.append(pe)
            continue
        if len(matches) > 1:
            pe.status = "ambiguous"
            pe.candidates = [m["id"] for m in matches]
            plan.entries.append(pe)
            continue
        row = matches[0]
        pe.resolved_id = row["id"]
        pe.prev_state = row["publication_state"]
        pe.pending_comments = _pending_comments(db, row["id"])
        if entry.target_state == "canonical" and pe.pending_comments > 0:
            pe.status = "blocked-comments"
        elif pe.prev_state == entry.target_state:
            pe.status = "already-at-target"
        else:
            pe.status = "ok"
        plan.entries.append(pe)
    return plan


# ── Execution ────────────────────────────────────────────────


@dataclass
class Transition:
    source_id: str
    prev_state: str
    new_state: str
    ts: str

    def to_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "prev_state": self.prev_state,
            "new_state": self.new_state,
            "ts": self.ts,
        }


@dataclass
class RunResult:
    transitions: list[Transition] = field(default_factory=list)
    skipped_already_at_target: list[PlanEntry] = field(default_factory=list)
    audit_note_id: str | None = None

    def to_dict(self) -> dict:
        return {
            "transitions": [t.to_dict() for t in self.transitions],
            "skipped_already_at_target": [
                {"resolved_id": e.resolved_id, "target_state": e.target_state}
                for e in self.skipped_already_at_target
            ],
            "audit_note_id": self.audit_note_id,
        }


class PromotionBlocked(RuntimeError):
    """Raised when a plan has blockers and ``force`` is not set."""

    def __init__(self, plan: Plan):
        self.plan = plan
        blockers = plan.blockers()
        msg = f"{len(blockers)} blocker(s) in plan — resolve before promoting"
        super().__init__(msg)


def execute(
    *,
    plan: Plan,
    caller_org: str | None,
    actor: str,
    db_path: str,
) -> RunResult:
    """Apply the plan and file the audit note.

    Opens its own DB connections (via ops) to avoid two-writer locking when
    a caller passes an already-open GraphDB. Raises :class:`PromotionBlocked`
    if the plan has blockers.
    """
    if plan.blockers():
        raise PromotionBlocked(plan)

    result = RunResult()
    for entry in plan.entries:
        if entry.status == "already-at-target":
            result.skipped_already_at_target.append(entry)
            continue
        if not entry.is_actionable():
            continue   # defensive; blockers() already caught these
        rec = ops.promote_source(
            entry.resolved_id,      # type: ignore[arg-type]
            entry.target_state,
            caller_org=caller_org,
        )
        result.transitions.append(
            Transition(
                source_id=rec["id"],
                prev_state=rec["prev_state"],
                new_state=rec["new_state"],
                ts=rec["ts"],
            )
        )

    audit_db = GraphDB(db_path)
    try:
        result.audit_note_id = _file_audit_note(
            db=audit_db,
            plan=plan,
            result=result,
            actor=actor,
            db_path=db_path,
        )
    finally:
        audit_db.close()
    return result


def _file_audit_note(
    *,
    db: GraphDB,
    plan: Plan,
    result: RunResult,
    actor: str,
    db_path: str,
) -> str:
    """Create a canonical note recording the promotion session.

    The note body is machine-readable (JSON block wrapped in a short prose
    header) so future librarians can diff successive audit notes without
    parsing free-form text.
    """
    ts = now_iso()
    body = (
        "Bootstrap allowlist promotion audit — "
        f"org={plan.org}, actor={actor}, ts={ts}.\n"
        "Charter: graph://93cf3026-1df.\n\n"
        "```json\n"
        + json.dumps(
            {
                "org": plan.org,
                "allowlist": plan.allowlist_path,
                "actor": actor,
                "ts": ts,
                "transitions": [t.to_dict() for t in result.transitions],
                "skipped_already_at_target": [
                    {"resolved_id": e.resolved_id, "target_state": e.target_state}
                    for e in result.skipped_already_at_target
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n```\n"
    )

    source_key = f"note:{new_id()}"
    source = Source(
        type="note",
        platform="local",
        project=None,
        title=f"Bootstrap allowlist audit — {plan.org} @ {ts}",
        file_path=source_key,
        metadata={
            "tags": ["curation", "audit", "publication-state", "autonomy-public-surface"],
            "author": actor,
            "curation_run": {
                "allowlist": plan.allowlist_path,
                "ts": ts,
                "transition_count": len(result.transitions),
            },
        },
        publication_state="canonical",
    )
    db.insert_source(source)
    thought = Thought(source_id=source.id, content=body, role="user", turn_number=1,
                      tags=["curation", "audit"])
    db.insert_thought(thought)
    db.insert_note_version(source.id, 1, body)
    db.commit()
    return source.id


# ── Rendering ────────────────────────────────────────────────


def render_plan(plan: Plan) -> str:
    lines = [f"# Promotion plan — org={plan.org}", f"# allowlist={plan.allowlist_path}", ""]
    status_buckets = ("ok", "already-at-target", "blocked-comments", "missing", "ambiguous")
    by_status = {s: [e for e in plan.entries if e.status == s] for s in status_buckets}
    for status in status_buckets:
        rows = by_status[status]
        lines.append(f"## {status} ({len(rows)})")
        if not rows:
            lines.append("  (none)")
            lines.append("")
            continue
        for e in rows:
            id_bit = e.resolved_id[:12] if e.resolved_id else e.prefix
            base = f"  {id_bit:<12}  → {e.target_state}"
            if status == "ok":
                base += f"  (was {e.prev_state})"
            elif status == "blocked-comments":
                base += f"  pending_comments={e.pending_comments}"
            elif status == "ambiguous":
                base += f"  candidates={','.join(c[:8] for c in e.candidates[:4])}"
            lines.append(base)
        lines.append("")
    return "\n".join(lines) + "\n"


def render_result(result: RunResult) -> str:
    lines = [
        f"# Promotion applied",
        f"transitions: {len(result.transitions)}",
        f"already-at-target: {len(result.skipped_already_at_target)}",
        f"audit_note_id: {result.audit_note_id}",
        "",
    ]
    for t in result.transitions:
        lines.append(f"  {t.source_id[:12]}  {t.prev_state} → {t.new_state}  @ {t.ts}")
    return "\n".join(lines) + "\n"


# ── CLI ──────────────────────────────────────────────────────


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tools.graph.curation.promote",
        description=(
            "Apply a bootstrap allowlist to an org DB "
            "(charter: graph://93cf3026-1df)."
        ),
    )
    p.add_argument("--allowlist", default=str(DEFAULT_AUTONOMY_PATH))
    p.add_argument("--db", default=None, help="Graph DB path (default: GRAPH_DB env or ops routing).")
    p.add_argument("--caller-org", default=None, help="Override caller_org for ops routing.")
    p.add_argument("--actor", default=None, help="Override actor name in the audit note.")
    p.add_argument("--dry-run", action="store_true", help="Print the plan; do not mutate.")
    p.add_argument("--json", action="store_true", help="JSON output instead of text.")
    return p


def _resolve_db_path(cli_db: str | None, caller_org: str | None) -> str:
    if cli_db:
        return cli_db
    env = os.environ.get("GRAPH_DB")
    if env:
        return env
    from tools.graph.ops import _db_path
    path = _db_path(caller_org)
    if not path:
        raise SystemExit("no GRAPH_DB configured and no --db given")
    return path


def run(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    allowlist = load_allowlist(args.allowlist)
    caller_org = args.caller_org or allowlist.org
    db_path = _resolve_db_path(args.db, caller_org)
    actor = args.actor or os.environ.get("BD_ACTOR") or os.environ.get("USER") or "librarian"

    # The CLI honours caller_org routing through ops for the mutation path,
    # but we still open a direct GraphDB for planning + audit-note insert —
    # both operations are DB-local and already scoped to the selected path.
    prev_env = os.environ.get("GRAPH_DB")
    os.environ["GRAPH_DB"] = db_path
    try:
        db = GraphDB(db_path)
        try:
            plan = build_plan(db=db, allowlist=allowlist)
        finally:
            db.close()

        if args.dry_run:
            output = (json.dumps(
                {"plan": [_plan_entry_dict(e) for e in plan.entries]}, indent=2)
                if args.json else render_plan(plan))
            sys.stdout.write(output)
            return 0 if not plan.blockers() else 2
        if plan.blockers():
            sys.stderr.write(render_plan(plan))
            sys.stderr.write(
                f"\nrefusing to promote: {len(plan.blockers())} blocker(s). "
                "Integrate pending comments or fix the allowlist, then retry.\n"
            )
            return 2
        try:
            result = execute(
                plan=plan, caller_org=caller_org,
                actor=actor, db_path=db_path,
            )
        except PromotionBlocked:
            sys.stderr.write(render_plan(plan))
            return 2
    finally:
        if prev_env is None:
            os.environ.pop("GRAPH_DB", None)
        else:
            os.environ["GRAPH_DB"] = prev_env

    output = (
        json.dumps({"plan": render_plan(plan), **result.to_dict()}, indent=2)
        if args.json else
        (render_plan(plan) + render_result(result))
    )
    sys.stdout.write(output)
    return 0


def _plan_entry_dict(e: PlanEntry) -> dict:
    return {
        "prefix": e.prefix,
        "target_state": e.target_state,
        "resolved_id": e.resolved_id,
        "prev_state": e.prev_state,
        "pending_comments": e.pending_comments,
        "status": e.status,
        "candidates": list(e.candidates),
    }


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run())
