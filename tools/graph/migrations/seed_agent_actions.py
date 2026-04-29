"""Seed ``dashboard.agent-actions#1`` Settings for the autonomy org.

Each entry produces one Setting member in autonomy's per-org DB. Other
orgs adopt actions via canonical promotion of these members into their
own DB; this migration only seeds autonomy.

Idempotent: rows already present (matching ``set_id + schema_revision +
key``) are skipped on re-run. Prompt templates load from
``agents/actions/<key>.md`` — a missing file aborts the migration loudly
rather than silently inserting a NULL prompt that would surface later as
a runtime error at dispatch time.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tools.graph.db import GraphDB
from tools.graph import schemas  # noqa: F401 — registers contracts
from tools.graph.org_ops import uuid7
from tools.graph.schemas.agent_actions import (
    AGENT_ACTIONS_REVISION,
    AGENT_ACTIONS_SET_ID,
)


REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ORGS_DIR = REPO_ROOT / "data" / "orgs"
DEFAULT_PROMPTS_DIR = REPO_ROOT / "agents" / "actions"
DEFAULT_TARGET_ORG = "autonomy"


# ── Seed table ──────────────────────────────────────────────

# Each entry is (key, partial_payload). The migration loads the prompt
# template from agents/actions/<key>.md at run time and inlines it under
# ``prompt_template`` before validating + inserting. Universal members
# omit ``prompt_template`` (their dispatch path is special-cased in the
# endpoint and does not spawn an agent).
SEEDS: tuple[tuple[str, dict], ...] = (
    (
        "session.send-to",
        {
            "asset_type": "*",
            "label": "Send To…",
            "icon": "↗",
            "universal": True,
            "writes": [],
        },
    ),
    (
        "note.update-summary",
        {
            "asset_type": "note",
            "label": "Update Title & Summary",
            "icon": "✏",
            "model": "claude-haiku-4-5-20251001",
            "estimated_seconds": 10,
            "writes": ["source.title", "source.short_description"],
        },
    ),
    (
        "note.consolidate-comments",
        {
            "asset_type": "note",
            "label": "Consolidate Comments",
            "icon": "⊞",
            "model": "claude-sonnet-4-6",
            "estimated_seconds": 30,
            "writes": ["note-version"],
        },
    ),
    (
        "note.review-accuracy",
        {
            "asset_type": "note",
            "label": "Review for Accuracy",
            "icon": "✓",
            "model": "claude-sonnet-4-6",
            "estimated_seconds": 60,
            "writes": ["comment"],
        },
    ),
)


# ── Helpers ─────────────────────────────────────────────────


def _load_prompt_template(key: str, prompts_dir: Path) -> str:
    """Load the prompt template for *key* from ``<prompts_dir>/<key>.md``.

    A missing or empty file is fatal: a NULL prompt would surface later as
    a runtime error at action dispatch time, so we fail the migration
    loudly instead.
    """
    path = prompts_dir / f"{key}.md"
    if not path.is_file():
        raise RuntimeError(
            f"agent-actions seed: prompt template missing for {key!r} at "
            f"{path}. Add the file or remove the action from the seed list."
        )
    text = path.read_text()
    if not text.strip():
        raise RuntimeError(
            f"agent-actions seed: prompt template for {key!r} is empty at "
            f"{path}."
        )
    return text


def _build_payload(
    partial: dict, *, key: str, prompts_dir: Path,
) -> dict:
    """Materialize the full Setting payload for one seed entry.

    Non-universal entries get ``prompt_template`` filled in from the
    matching ``<key>.md`` file. Universal entries skip the prompt load —
    they are special-cased at dispatch time and never reach the agent
    spawn path.
    """
    payload: dict[str, Any] = dict(partial)
    if not payload.get("universal"):
        payload["prompt_template"] = _load_prompt_template(key, prompts_dir)
    return payload


def _setting_exists(
    db: GraphDB, *, set_id: str, schema_revision: int, key: str,
) -> bool:
    row = db.conn.execute(
        "SELECT 1 FROM settings WHERE set_id = ? AND schema_revision = ? "
        "AND key = ? LIMIT 1",
        (set_id, int(schema_revision), key),
    ).fetchone()
    return row is not None


def _fetch_setting(
    db: GraphDB, *, set_id: str, schema_revision: int, key: str,
) -> dict | None:
    row = db.conn.execute(
        "SELECT id, payload, updated_at FROM settings "
        "WHERE set_id = ? AND schema_revision = ? AND key = ? LIMIT 1",
        (set_id, int(schema_revision), key),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "payload": row["payload"],
        "updated_at": row["updated_at"],
    }


def _parse_iso_to_epoch(ts: str | None) -> float:
    """Best-effort ISO-8601 → epoch seconds. Returns 0 on parse failure."""
    if not ts:
        return 0.0
    try:
        # The seed writes Z-suffixed UTC timestamps; fromisoformat doesn't
        # accept the trailing Z until 3.11+, so normalise it.
        normalised = ts.rstrip("Z")
        dt = datetime.fromisoformat(normalised)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return 0.0


# Pre-rename keys whose payload moved to a new key in the same set. The
# seed loop is idempotent on the new key; the old row needs explicit
# retirement so the dropdown stops surfacing it. Deprecating (rather than
# deleting) preserves any provenance that referred to the legacy id.
LEGACY_RETIREMENTS: tuple[tuple[str, int, str], ...] = (
    ("dashboard.agent-actions", 1, "universal.send-to"),
)


def _retire_legacy_members(db: GraphDB, *, log) -> None:
    for set_id, revision, key in LEGACY_RETIREMENTS:
        cur = db.conn.execute(
            "UPDATE settings SET deprecated = 1, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
            "WHERE set_id = ? AND schema_revision = ? AND key = ? "
            "AND deprecated = 0",
            (set_id, int(revision), key),
        )
        if cur.rowcount:
            db.conn.commit()
            log(f"  retired legacy {key!r} ({cur.rowcount} row)")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Plan / Apply ───────────────────────────────────────────


def build_plan(
    org_db: Path, *,
    prompts_dir: Path = DEFAULT_PROMPTS_DIR,
    no_update: bool = False,
) -> list[dict]:
    """Validate every seed payload + decide which need inserting/updating.

    Returns a list of ``{key, payload, action}`` dicts where ``action`` is
    ``insert``, ``update``, or ``skip_exists``. ``update`` fires when the
    prompt-template file's mtime is newer than the row's ``updated_at`` AND
    the rendered payload differs from the stored one — matches operator
    intuition (edit the file, run the migration, see the new content).
    ``no_update=True`` (CLI: ``--no-update``) pins existing rows so the
    migration only inserts. Raises if the org DB is missing, a payload
    fails schema validation, or a prompt template is missing.
    """
    if not org_db.exists():
        raise RuntimeError(
            f"agent-actions seed: target org DB not found: {org_db}. Run "
            f"the org-bootstrap migration first."
        )

    plan: list[dict] = []
    db = GraphDB(org_db)
    try:
        for key, partial in SEEDS:
            payload = _build_payload(
                partial, key=key, prompts_dir=prompts_dir,
            )
            schemas.validate_payload(
                AGENT_ACTIONS_SET_ID, AGENT_ACTIONS_REVISION, payload,
            )
            existing = _fetch_setting(
                db,
                set_id=AGENT_ACTIONS_SET_ID,
                schema_revision=AGENT_ACTIONS_REVISION,
                key=key,
            )
            entry: dict[str, object] = {"key": key, "payload": payload}
            if existing is None:
                entry["action"] = "insert"
            elif no_update:
                entry["action"] = "skip_exists"
                entry["existing_id"] = existing["id"]
            else:
                file_path = prompts_dir / f"{key}.md"
                file_mtime = (
                    file_path.stat().st_mtime if file_path.is_file() else 0.0
                )
                row_mtime = _parse_iso_to_epoch(existing["updated_at"])
                payload_changed = (
                    json.loads(existing["payload"]) != payload
                )
                if payload_changed and file_mtime > row_mtime:
                    entry["action"] = "update"
                    entry["existing_id"] = existing["id"]
                else:
                    entry["action"] = "skip_exists"
                    entry["existing_id"] = existing["id"]
            plan.append(entry)
    finally:
        db.close()
    return plan


def apply_plan(
    org_db: Path, plan: list[dict], *, log=print,
) -> list[dict]:
    """Insert/update every entry in *plan* per its ``action``.

    Re-checks idempotency inside the write loop so concurrent runs do not
    double-insert. Returns the same plan (with ``action`` flipped to
    ``skip_exists`` for any row racing in).
    """
    db = GraphDB(org_db)
    try:
        _retire_legacy_members(db, log=log)
        for entry in plan:
            action = entry["action"]
            if action == "skip_exists":
                log(f"  {entry['key']}: skip (skip_exists)")
                continue
            if action == "update":
                existing_id = entry.get("existing_id")
                if not existing_id:
                    log(f"  {entry['key']}: skip (update without id)")
                    continue
                now = _now_iso()
                db.conn.execute(
                    "UPDATE settings SET payload = ?, updated_at = ? "
                    "WHERE id = ?",
                    (json.dumps(entry["payload"]), now, existing_id),
                )
                db.conn.commit()
                log(f"  {entry['key']}: updated ({existing_id})")
                continue
            # action == "insert"
            if _setting_exists(
                db,
                set_id=AGENT_ACTIONS_SET_ID,
                schema_revision=AGENT_ACTIONS_REVISION,
                key=entry["key"],
            ):
                entry["action"] = "skip_exists"
                log(f"  {entry['key']}: skip (raced)")
                continue
            sid = uuid7()
            now = _now_iso()
            db.conn.execute(
                "INSERT INTO settings(id, set_id, schema_revision, key, "
                "payload, publication_state, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    sid,
                    AGENT_ACTIONS_SET_ID,
                    AGENT_ACTIONS_REVISION,
                    entry["key"],
                    json.dumps(entry["payload"]),
                    "canonical",
                    now,
                    now,
                ),
            )
            db.conn.commit()
            log(f"  {entry['key']}: inserted ({sid})")
    finally:
        db.close()
    return plan


def run(
    *,
    org: str = DEFAULT_TARGET_ORG,
    orgs_dir: Path = DEFAULT_ORGS_DIR,
    prompts_dir: Path = DEFAULT_PROMPTS_DIR,
    dry_run: bool = False,
    no_update: bool = False,
    log=print,
) -> list[dict]:
    """Public entry point. Builds and (optionally) applies the seed plan.

    ``no_update=True`` pins existing rows; the migration only inserts new
    ones. Otherwise rows whose prompt file is newer than the stored
    ``updated_at`` get an UPDATE.
    """
    org_db = orgs_dir / f"{org}.db"
    plan = build_plan(org_db, prompts_dir=prompts_dir, no_update=no_update)
    inserts = sum(1 for e in plan if e["action"] == "insert")
    updates = sum(1 for e in plan if e["action"] == "update")
    skips = sum(1 for e in plan if e["action"] == "skip_exists")
    log(
        f"agent-actions seed for {org!r}: "
        f"{inserts} insert, {updates} update, {skips} skip"
    )
    if dry_run:
        return plan
    return apply_plan(org_db, plan, log=log)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", default=DEFAULT_TARGET_ORG)
    parser.add_argument(
        "--orgs-dir", type=Path, default=DEFAULT_ORGS_DIR,
        help="root containing per-org DBs (default: data/orgs/)",
    )
    parser.add_argument(
        "--prompts-dir", type=Path, default=DEFAULT_PROMPTS_DIR,
        help="root containing <key>.md templates (default: agents/actions/)",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--no-update", action="store_true",
        help="pin existing rows; only insert new ones (skip mtime-based "
             "auto-update). Use when payload pinning is intentional.",
    )
    args = parser.parse_args(argv)
    try:
        run(
            org=args.org,
            orgs_dir=args.orgs_dir,
            prompts_dir=args.prompts_dir,
            dry_run=args.dry_run,
            no_update=args.no_update,
        )
    except Exception as e:
        print(f"agent-actions seed: aborted: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
