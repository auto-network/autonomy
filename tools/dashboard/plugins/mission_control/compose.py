"""Compose a Mission screen into one document.

One function, :func:`compose_screen`, is called by both surfaces: the relay
resolver in ``link_serving`` and Mission Control's own HTTP serving. A screen
is the mission overview or any one pillar; both compose identically, which is
why navigating between them can be a plain document swap in the viewer.

The composition is CONCATENATION. The coordinator's stored HTML goes in byte
for byte -- nothing here parses, rewrites, regexes or reserialises it. The
browser parses the result once, normally, so the author's scripts run, their
DOMContentLoaded and load fire, and their in-page anchors resolve.
"""

from __future__ import annotations

import json
import time

from tools.dashboard.dao import mission_control_db as db
from tools.dashboard.scripts.build_mission_viewer import bootstrap_source

#: ``<base href="about:srcdoc">`` is what makes ``#fragment`` links resolve
#: in-document inside a sandboxed srcdoc frame; without it they resolve
#: against the bootloader's URL and navigate the frame away. The registry
#: serves ``base-uri about:`` for it -- under ``base-uri 'none'`` the browser
#: ignores the element silently, with nothing in the console.
_HEAD = '<!doctype html>\n<base href="about:srcdoc">\n'


def _ago(then: float | None, now: float) -> str:
    """A duration a human reads at a glance: 40s, 12m, 4h, 3d."""
    if not then:
        return ""
    seconds = max(0, int(now - then))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _initial(label: str | None) -> str:
    text = (label or "").strip()
    return text[0].upper() if text else "?"


def _presence(surface_id: str, now: float) -> list[dict]:
    """Who is on one surface right now.

    Rows are keyed ``{surface_id}:{participant_id}`` and persist after the
    person leaves, so ``state`` is what decides who is HERE -- not the row's
    existence. Presence is decoration: a failure to read it must never fail
    the screen.
    """
    from tools.graph import settings_ops
    from tools.graph.surface import SURFACE_PRESENCE_SET_ID

    prefix = f"{surface_id}:"
    try:
        rows = settings_ops.read_set(SURFACE_PRESENCE_SET_ID)
    except Exception:
        return []
    here = []
    for member in rows.members:
        if not isinstance(member.key, str) or not member.key.startswith(prefix):
            continue
        payload = member.payload if isinstance(member.payload, dict) else {}
        if payload.get("state") != "active":
            continue
        label = payload.get("participant_label")
        here.append({
            "participant_id": payload.get("participant_id"),
            "label": label,
            "initial": _initial(label),
            "kind": payload.get("participant_kind"),
            "seen": _ago(payload.get("heartbeat_at"), now),
        })
    return here


def _pillar_state(pillar: dict, now: float) -> dict:
    """One pillar as the chrome needs it.

    ``age`` is time since the coordinator last pushed this pillar's site.
    ``last_done`` is the only field here a human wrote: the last productive
    thing that finished, in their own words. It is passed through verbatim
    and NEVER substituted for -- no status value, no revision note, no
    inference. A pillar whose coordinator has not written one renders
    nothing there, which is honest; a plausible guess would not be.
    """
    pillar_id = pillar["pillar_id"]
    current = db.get_current_pillar_site(pillar_id)
    return {
        "pillar_id": pillar_id,
        "name": pillar["name"],
        "color": pillar["color"],
        "status": pillar["status"],
        "age": _ago((current or {}).get("created_at"), now),
        "open": db.count_open_pillar_questions(pillar_id),
        "last_done": pillar["last_done"],
        "here": _presence(f"pillar:{pillar_id}", now),
    }


def _question_state(entry: dict) -> dict:
    """One conversation entry, trimmed to what the chrome renders.

    Interim updates are working noise, not the record: once an entry is
    answered they are dropped, matching ``_question_payload`` on the API
    side and ``reopen_question`` on the write side.
    """
    updates = (
        []
        if entry["answer"] is not None
        else [
            {"update_id": u["update_id"], "text": u["text"]}
            for u in db.list_conversation_updates(entry["entry_id"])
        ]
    )
    return {
        "entry_id": entry["entry_id"],
        "pillar_id": entry.get("pillar_id"),
        "anchor": entry.get("anchor"),
        "question": entry["question"],
        "asked_by_label": entry["asked_by_label"],
        "answer": entry["answer"],
        "answered_at": entry["answered_at"],
        "created_at": entry["created_at"],
        "updates": updates,
    }


def mission_state(mission_id: str, pillar_id: str | None = None) -> dict:
    """The whole state block for one screen.

    Everything in one object because the whole artifact already arrives in
    one channel message -- splitting it into parts would add round trips
    without removing any bytes.
    """
    now = time.time()
    pillars = [_pillar_state(p, now) for p in db.list_pillars(mission_id)]
    return {
        "mission_id": mission_id,
        "screen": pillar_id,
        "pillars": pillars,
        "questions": [
            _question_state(e)
            for e in db.list_whole_mission_conversation(mission_id)
        ],
        "here": _presence(f"mission:{mission_id}", now),
    }


def _state_block(state: dict) -> str:
    """The state as an inert JSON block.

    ``</script`` is the only sequence that can end the block early, and the
    HTML parser matches it case-insensitively, so it is the one thing that
    has to be neutralised. JSON string escapes are the encoding, which keeps
    the block valid JSON for the ``JSON.parse`` on the other side.
    """
    text = json.dumps(state, separators=(",", ":"))
    text = text.replace("</", "<\\/")
    return f'<script type="application/json" id="mc-state">{text}</script>\n'


def compose_screen(mission_id: str, pillar_id: str | None = None) -> bytes | None:
    """One screen as a complete document, or None if there is nothing to serve.

    Order matters: the state block and the bootstrap precede the author's
    HTML so the runtime is mounted before their scripts run.
    """
    if pillar_id is None:
        current = db.get_current_site(mission_id)
    else:
        current = db.get_current_pillar_site(pillar_id)
    if not current or not current.get("html"):
        return None
    document = (
        _HEAD
        + _state_block(mission_state(mission_id, pillar_id))
        + "<script>\n" + bootstrap_source() + "\n</script>\n"
        + current["html"]          # byte for byte, never parsed
    )
    return document.encode("utf-8")
