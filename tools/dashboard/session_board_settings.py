"""Settings schemas behind the Session Board (/sessions/board).

Two sets, both homed in the operator's ``personal`` store so that fleet sync
replicates them and every desktop shows the same board:

``dashboard.session.group`` — a session group, one member per slug. A group
is sessions working one effort right now: its title is the board column's
title, ``members`` is who sits in that column. Agents write their own
membership from ``graph group``; the operator writes it by dragging a card.
Both arrive through server routes that act on the caller's behalf in the
personal store (an org-bound agent token cannot select ``personal`` itself).

``dashboard.session.board.layout`` — the operator's arrangement of the
board: presentation, column order, column widths, card heights. One member
per operator; the dashboard is single-operator today, so the key is
``default``.

Durable operator state lives here and nowhere else. ``dashboard.db`` and
browser storage never hold a copy of these records (see the workspace primer
block ``settings-first``).
"""
from __future__ import annotations

import re
import time

from tools.graph import settings_ops
from tools.graph.schemas.registry import (
    SettingSchema,
    field,
    home,
    keyed_per_entity,
    publication_band,
    singleton,
)

GROUP_SET_ID = "dashboard.session.group"
LAYOUT_SET_ID = "dashboard.session.board.layout"
SCHEMA_REVISION = 1
STORE = "personal"
LAYOUT_KEY = "default"
_STATE = "raw"

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")

SYNOPSIS = {
    GROUP_SET_ID: (
        "Session groups — the columns of the Session Board. One member per "
        "group slug: title, colour, purpose, rationale, and the sessions that "
        "belong to it (one group per live session). Written by agents through "
        "`graph group` and by the operator by dragging cards; the dashboard "
        "server writes on their behalf into the personal store so the record "
        "replicates across the fleet."
    ),
    LAYOUT_SET_ID: (
        "The operator's arrangement of the Session Board: card presentation, "
        "column order, column widths and card heights. One member per operator "
        "(key `default`), replicated by fleet sync so every desktop shows the "
        "same board."
    ),
}


@publication_band(min="raw", max="canonical")
@home(STORE)
@keyed_per_entity(key_strategy="group_slug")
class SessionGroupV1(SettingSchema):
    """One session group. The key is the slug that `graph group join <slug>`
    and `graph crosstalk send group:<slug>` use."""

    set_id = GROUP_SET_ID
    schema_revision = SCHEMA_REVISION

    name: str = field(required=True, description="Group title, in the operator's words; the board column's title.")
    short: str = field(default="", description="Short name (up to 12 characters) for chips and CrossTalk tabs.")
    color: str = field(default="", description="Hex colour for the column swatch; empty means the members' org colour.")
    purpose: str = field(default="", description="One sentence: what the lane delivers.")
    why: str = field(default="", description="One sentence of rationale, shown under the column title (the auto-organize librarian writes this).")
    coordinator_session: str = field(default="", description="Session that speaks for the group, if any.")
    refs: list = field(default_factory=list, element=str, description="References such as bead:auto-xxxx or mission:<uuid>.")
    members: list = field(
        default_factory=list, element=dict,
        description="Sessions in the group: [{session, tab, joined_at, joined_by}]. A session is in at most one group.",
    )
    created_at: float = field(default=0.0, description="Unix time the group was created.")
    created_by: str = field(default="", description="Session name or 'operator' that created the group.")
    updated_at: float = field(default=0.0, description="Unix time of the last write.")


@publication_band(min="raw", max="canonical")
@home(STORE)
@singleton(key=LAYOUT_KEY)
class SessionBoardLayoutV1(SettingSchema):
    """The operator's board arrangement. Membership is NOT here — columns are
    derived from the groups; this only says how they are laid out."""

    set_id = LAYOUT_SET_ID
    schema_revision = SCHEMA_REVISION

    presentation: str = field(default="transcript", enum=["transcript", "stats"], description="Default card presentation for cards the operator has not set individually.")
    presentations: dict = field(default_factory=dict, description="Per-card face by session name: 'transcript' or 'stats'. Only cards the operator flipped individually.")
    column_order: list = field(default_factory=list, element=str, description="Group slugs left to right; 'solo' is the Ungrouped column.")
    widths: dict = field(default_factory=dict, description="Column width in CSS px by slug, only for columns the operator resized.")
    heights: dict = field(default_factory=dict, description="Card height in CSS px by session name, only for cards the operator resized.")
    focus_session: str = field(default="", description="The session shown full height in its column, or empty. Its column is wherever that session sits.")
    updated_at: float = field(default=0.0, description="Unix time of the last write.")


# ── Groups ──────────────────────────────────────────────────────────────

_GROUP_FIELDS = ("name", "short", "color", "purpose", "why", "coordinator_session", "refs")
_SUMMARY_FIELDS = ("slug", "name", "short", "color", "why")


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-")[:40]
    return slug or f"g-{int(time.time()) % 100000:05d}"


def _rows() -> dict[str, dict]:
    """slug → stored payload (own store, every state)."""
    out = {}
    for member in settings_ops.read_set(GROUP_SET_ID, org=STORE, peers=[]).members:
        payload = dict(member.payload or {})
        payload["slug"] = member.key
        payload["_id"] = member.id
        out[member.key] = payload
    return out


def _public(row: dict) -> dict:
    return {k: v for k, v in row.items() if k != "_id"}


def _write(slug: str, payload: dict) -> dict:
    payload = {k: v for k, v in payload.items() if k not in ("slug", "_id")}
    payload["updated_at"] = time.time()
    settings_ops.write_by_key(GROUP_SET_ID, SCHEMA_REVISION, slug, payload, org=STORE, state=_STATE)
    return get_group(slug)


def list_groups() -> list[dict]:
    rows = sorted(_rows().values(), key=lambda r: (r.get("created_at") or 0, r["slug"]))
    return [_public(r) for r in rows]


def get_group(slug: str) -> dict | None:
    row = _rows().get(slug)
    return _public(row) if row else None


def upsert_group(slug: str, fields: dict, created_by: str = "") -> dict:
    """Create or update a group. Only the group's own fields are written here;
    membership goes through :func:`set_session_group`."""
    existing = _rows().get(slug)
    data = {k: fields[k] for k in _GROUP_FIELDS if k in fields and fields[k] is not None}
    if existing is None:
        payload = {"name": (data.get("name") or slug).strip() or slug, "created_at": time.time(), "created_by": created_by, "members": []}
        payload.update({k: v for k, v in data.items() if k != "name"})
    else:
        payload = dict(existing)
        payload.update(data)
    return _write(slug, payload)


def delete_group(slug: str) -> int:
    """Dissolve a group. Returns how many sessions it released."""
    row = _rows().get(slug)
    if not row:
        return 0
    released = len(row.get("members") or [])
    settings_ops.remove_setting(row["_id"], org=STORE)
    return released


def group_members(slug: str, live_only: bool = True) -> list[str]:
    row = _rows().get(slug)
    if not row:
        return []
    names = [m.get("session") for m in (row.get("members") or []) if m.get("session")]
    if live_only:
        live = _live_session_names()
        if live is not None:
            names = [n for n in names if n in live]
    return names


def _live_session_names() -> set[str] | None:
    try:
        from tools.dashboard.dao import dashboard_db
        return {s["tmux_name"] for s in dashboard_db.get_live_sessions()}
    except Exception:
        return None


def set_session_group(session: str, group_id: str | None, tab: str = "", joined_by: str = "") -> None:
    """Place ``session`` in ``group_id`` (or nowhere). One group per session:
    it is removed from any other group in the same write."""
    rows = _rows()
    for slug, row in rows.items():
        members = row.get("members") or []
        kept = [m for m in members if m.get("session") != session]
        if slug == group_id:
            kept.append({"session": session, "tab": (tab or "")[:12], "joined_at": time.time(), "joined_by": joined_by or ""})
        if len(kept) != len(members) or slug == group_id:
            row = dict(row)
            row["members"] = kept
            _write(slug, row)


def group_summaries() -> dict[str, dict]:
    """slug → {slug, name, short, color, why}: what the registry carries per session."""
    return {slug: {k: row.get(k, "") for k in _SUMMARY_FIELDS} for slug, row in _rows().items()}


def session_group_index() -> dict[str, dict]:
    """session name → {group_id, group_tab, group}: the per-session projection
    the registry and the active-sessions DAO stamp onto every row."""
    out: dict[str, dict] = {}
    for slug, row in _rows().items():
        summary = {k: row.get(k, "") for k in _SUMMARY_FIELDS}
        for m in row.get("members") or []:
            name = m.get("session")
            if not name:
                continue
            out[name] = {"group_id": slug, "group_tab": (m.get("tab") or "")[:12], "group": summary}
    return out


# ── Layout ──────────────────────────────────────────────────────────────

_LAYOUT_FIELDS = ("presentation", "presentations", "column_order", "widths", "heights", "focus_session")


def read_layout(key: str = LAYOUT_KEY) -> dict:
    row = settings_ops.read_set_key(LAYOUT_SET_ID, key, org=STORE, peers=[])
    payload = dict((row or {}).get("payload") or {})
    return {
        "presentation": payload.get("presentation") or "transcript",
        "presentations": dict(payload.get("presentations") or {}),
        "column_order": list(payload.get("column_order") or []),
        "widths": dict(payload.get("widths") or {}),
        "heights": dict(payload.get("heights") or {}),
        "focus_session": payload.get("focus_session") or "",
        "updated_at": payload.get("updated_at") or 0,
    }


def write_layout(fields: dict, key: str = LAYOUT_KEY) -> dict:
    current = read_layout(key)
    for k in _LAYOUT_FIELDS:
        if k in fields and fields[k] is not None:
            current[k] = fields[k]
    current["updated_at"] = time.time()
    settings_ops.write_by_key(LAYOUT_SET_ID, SCHEMA_REVISION, key, current, org=STORE, state=_STATE)
    return read_layout(key)
