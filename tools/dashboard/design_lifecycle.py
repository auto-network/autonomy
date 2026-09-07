"""Design Studio lifecycle: designs are active or archived, and archiving is
automatic.

The catalog used to ask the operator to "complete" or "close" every design
by hand, and with a few hundred designs nobody kept up: the nav badge was a
permanent three-digit number.  Now:

* A design is **active** while it is being worked on, and **archived** once
  it has gone quiet.  The two legacy terminal statuses (``dismissed`` from
  the close button, ``completed`` from the old A/B ranking flow) both read
  as archived; nothing new is ever written as ``completed``.
* :func:`sweep` archives every active design whose latest revision is older
  than :data:`ARCHIVE_AFTER_DAYS`, **unless** the session that pushed it is
  still live or the design is shared (an active link grant reaches it).
  Restoring is one click and a restored design gets a fresh grace period,
  because the sweep only looks at the latest revision's age.
* :func:`live_design_count` is what the nav badge shows: designs a live
  session is working on right now, which is zero when nothing is happening.

The sweep runs at dashboard startup and hourly (``server.py``); it is also
safe to run by hand.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

ARCHIVE_AFTER_DAYS = 21
SWEEP_INTERVAL_SECONDS = 3600
ARCHIVED_STATUSES = ("dismissed", "completed")


def parse_created_at(value: object) -> datetime | None:
    """``designs.created_at`` is SQLite ``datetime('now')`` text (UTC, no
    zone); API clients sometimes write ISO with ``T``/``Z``."""
    raw = str(value or "").strip()
    if not raw:
        return None
    if "T" not in raw:
        raw = raw.replace(" ", "T", 1)
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def live_session_ids() -> set[str]:
    try:
        from tools.dashboard.dao import dashboard_db

        return {
            str(row.get("tmux_name") or "")
            for row in dashboard_db.get_live_sessions()
            if row.get("tmux_name")
        }
    except Exception:
        logger.debug("design-lifecycle: live sessions unavailable", exc_info=True)
        return set()


def _series() -> dict[str, list[dict]]:
    from agents.design_db import _get_conn

    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT id, COALESCE(design_id, id) AS design_id, status, created_at, "
            "creator_session_id, org, COALESCE(revision_seq, 1) AS revision_seq FROM designs"
        ).fetchall()
    finally:
        conn.close()
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        item = {k: row[k] for k in row.keys()}
        grouped[str(item["design_id"])].append(item)
    for revisions in grouped.values():
        revisions.sort(key=lambda r: (int(r.get("revision_seq") or 1), str(r.get("created_at") or "")))
    return grouped


def active_series() -> dict[str, list[dict]]:
    """Series with at least one pending revision."""
    return {
        design_id: revisions
        for design_id, revisions in _series().items()
        if any(r.get("status") == "pending" for r in revisions)
    }


def live_design_count(*, live: set[str] | None = None) -> int:
    """Active designs whose most recent contributing session is live."""
    live = live_session_ids() if live is None else live
    if not live:
        return 0
    count = 0
    for revisions in active_series().values():
        session = next(
            (r.get("creator_session_id") for r in reversed(revisions) if r.get("creator_session_id")),
            "",
        )
        if session in live:
            count += 1
    return count


def sweep(
    *,
    now: datetime | None = None,
    max_idle_days: int = ARCHIVE_AFTER_DAYS,
    live: set[str] | None = None,
    shared: dict[str, set[str]] | None = None,
) -> dict:
    """Archive quiet designs.  Returns ``{"archived": [...], "kept_live": n,
    "kept_shared": n, "checked": n}``.

    ``shared`` maps org slug -> ids with an active grant; when omitted it is
    read from the org's link grants for each org that appears.
    """
    from agents.design_db import _get_conn
    from tools.dashboard import design_shares

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max_idle_days)
    live = live_session_ids() if live is None else live
    shared = dict(shared) if shared is not None else {}
    archived: list[str] = []
    kept_live = kept_shared = 0
    checked = 0
    to_archive: list[str] = []
    for design_id, revisions in active_series().items():
        checked += 1
        latest = revisions[-1]
        created = parse_created_at(latest.get("created_at"))
        if created is None or created > cutoff:
            continue
        sessions = {r.get("creator_session_id") for r in revisions if r.get("creator_session_id")}
        if sessions & live:
            kept_live += 1
            continue
        org = next((r.get("org") for r in reversed(revisions) if r.get("org")), None) or "autonomy"
        if org not in shared:
            shared[org] = design_shares.shared_design_ids(org, now=now)
        ids = {design_id} | {str(r.get("id")) for r in revisions}
        if ids & shared[org]:
            kept_shared += 1
            continue
        to_archive.append(design_id)
    if to_archive:
        conn = _get_conn()
        try:
            for design_id in to_archive:
                conn.execute(
                    "UPDATE designs SET status = 'dismissed' "
                    "WHERE COALESCE(design_id, id) = ? AND status = 'pending'",
                    (design_id,),
                )
            conn.commit()
        finally:
            conn.close()
        archived = to_archive
        logger.info(
            "design-lifecycle: archived %d quiet design(s) (kept %d live, %d shared)",
            len(archived), kept_live, kept_shared,
        )
    return {"archived": archived, "kept_live": kept_live, "kept_shared": kept_shared, "checked": checked}
