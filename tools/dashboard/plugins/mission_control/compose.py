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

#: The org whose Settings store holds mission presence.
#:
#: Both the writer (a coordinator heartbeat on push or answer) and the reader
#: (this composer, and the API's presence endpoint) MUST name the same one.
#: The writer used to hardcode it while the readers used the caller-derived
#: sentinel, which resolves through a contextvar, then GRAPH_ORG, then None --
#: so in a process with no GRAPH_ORG the two sides used different databases
#: and the bar said "nobody here" while the rows sat exactly where they were
#: written. See graph://53f7412f-51e for the general shape of that hazard;
#: the guard there does not cover it, because the side that opted out of the
#: cascade was the WRITER.
#:
#: A literal, and honestly so: missions are not org-scoped -- there is one
#: mission_control.db and no org column on a mission -- so their presence
#: surface cannot be either. When a mission carries an org, this derives from
#: the mission and stops being a constant.
PRESENCE_ORG = "autonomy"

#: ``<base href="about:srcdoc">`` is what makes ``#fragment`` links resolve
#: in-document inside a sandboxed srcdoc frame; without it they resolve
#: against the bootloader's URL and navigate the frame away. The registry
#: serves ``base-uri about:`` for it -- under ``base-uri 'none'`` the browser
#: ignores the element silently, with nothing in the console.
_HEAD = (
    "<!doctype html>\n"
    # These screens are read on phones more than anywhere else. Without this
    # a mobile browser lays the page out at a notional desktop width and then
    # scales it down, which is why an author who forgets it gets a document
    # that pans sideways. Emitted by the platform so no coordinator has to
    # remember, and harmless when they declare their own -- first one wins.
    '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
    # One oversized element -- a screenshot, a wide table -- otherwise widens
    # the whole DOCUMENT, and then everything pans sideways together: the
    # prose, the gutters, and any bar positioned within the document. These
    # rules keep the overflow inside the element that has it. They are the
    # platform's only opinion about the author's content, and they set no
    # colours, fonts or spacing.
    "<style>\n"
    "  html { overflow-x: hidden; }\n"
    "  img, svg, video, canvas, iframe { max-width: 100%; height: auto; }\n"
    "  pre { max-width: 100%; overflow-x: auto; }\n"
    "  table { display: block; max-width: 100%; overflow-x: auto; }\n"
    "</style>\n"
)


def _ago(then, now: float) -> str:
    """A duration a human reads at a glance: 40s, 12m, 4h, 3d.

    Accepts either epoch seconds or an ISO 8601 timestamp, because the two
    sources here genuinely differ: revision rows carry epoch floats, and
    presence rows carry strings like "2026-08-12T18:34:18Z". Doing arithmetic
    on the string raises, and it raises INSIDE the loop that builds the
    who-list -- so the first time presence actually returned a row, every
    screen would have failed to render.
    """
    if not then:
        return ""
    if isinstance(then, str):
        from datetime import datetime, timezone
        try:
            parsed = datetime.fromisoformat(then.replace("Z", "+00:00"))
        except ValueError:
            return ""
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        then = parsed.timestamp()
    try:
        seconds = max(0, int(now - float(then)))
    except (TypeError, ValueError):
        return ""
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
        # org= is REQUIRED and has no default. Omitting it raises TypeError,
        # and a bare `except Exception` here turned that into "nobody is ever
        # here" -- silently, forever, on every screen. Presence being
        # decoration is a reason to degrade, never a reason not to look.
        rows = settings_ops.read_set(
            SURFACE_PRESENCE_SET_ID, org=PRESENCE_ORG,
        )
    except (LookupError, OSError, ValueError):
        return []          # store genuinely unavailable: render nobody
    here = []
    for member in rows.members:
        if not isinstance(member.key, str) or not member.key.startswith(prefix):
            continue
        payload = member.payload if isinstance(member.payload, dict) else {}
        label = payload.get("participant_label")
        here.append({
            "participant_id": payload.get("participant_id"),
            "label": label,
            "initial": _initial(label),
            "kind": payload.get("participant_kind"),
            "seen": _ago(payload.get("heartbeat_at"), now),
        })
    return here


def _latest_sign_of_life(current: dict | None, pillar: dict) -> float | None:
    """The more recent of the last site push and the last status line.

    Either may be absent -- a pillar can post before it ever pushes, or push
    without ever writing a line -- so this returns whichever exists, and None
    only when neither does.
    """
    stamps = [t for t in ((current or {}).get("created_at"), pillar.get("last_done_at"))
              if isinstance(t, (int, float))]
    return max(stamps) if stamps else None


def _pillar_state(pillar: dict, now: float) -> dict:
    """One pillar as the chrome needs it.

    ``age`` is time since this pillar last showed a sign of life -- the more
    recent of its last site push and its last status line. Reading only the
    push made a pillar that had been reporting steadily for an hour look
    untouched since its last revision, which is the opposite of the truth.
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
        "age": _ago(_latest_sign_of_life(current, pillar), now),
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


def mission_state(mission_id: str, pillar_id: str | None = None, *,
                  include_sessions: bool = False) -> dict:
    """The whole state block for one screen.

    Everything in one object because the whole artifact already arrives in
    one channel message -- splitting it into parts would add round trips
    without removing any bytes.
    """
    now = time.time()
    mission = db.get_mission(mission_id) or {}
    pillar_rows = db.list_pillars(mission_id)
    pillars = [_pillar_state(p, now) for p in pillar_rows]
    # WHERE A FILE GOES, ON THE DASHBOARD ONLY. Sending an attachment means
    # addressing the coordinator's own session, and the session name is
    # already ordinary furniture on the dashboard -- every session is listed
    # by name there. Over a share link it is not: a guest has no use for it,
    # cannot reach the upload endpoint anyway, and should not be handed the
    # internal name of a machine session to go with the screen they were
    # invited to read.
    if include_sessions:
        by_id = {p["pillar_id"]: p for p in pillar_rows}
        for entry in pillars:
            entry["coordinator_session"] = (
                by_id.get(entry["pillar_id"], {}).get("coordinator_session") or "")
    return {
        "mission_id": mission_id,
        "coordinator_session": (
            mission.get("coordinator_session") or "" if include_sessions else ""),
        # The overview screen is not "Mission" -- it has a name, and the bar
        # is where a reader confirms which mission they are looking at.
        "mission": mission.get("name") or "",
        "screen": pillar_id,
        "pillars": pillars,
        # Retired entries leave the screen. They stay in the record and in
        # the API listing -- what was asked, and why it stopped mattering, is
        # worth keeping -- but a reader looking at the work now should not
        # have to read past questions about subjects that no longer exist.
        "questions": [
            _question_state(e)
            for e in db.list_whole_mission_conversation(mission_id)
            if not e.get("retired_at")
        ],
        "here": _presence(f"mission:{mission_id}", now),
        # Every pillar's status line, newest first, with the time already
        # rendered. Inlined with the rest of the state because it is small
        # and a reader opening the feed should not wait on a round trip.
        "status_posts": [
            {
                "post_id": post["post_id"],
                "pillar_id": post["pillar_id"],
                "pillar_name": post["pillar_name"],
                "color": post["pillar_color"],
                "text": post["text"],
                "ago": _ago(post["created_at"], now),
            }
            for post in db.list_mission_status_posts(mission_id, limit=200)
        ],
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


#: <base href="about:srcdoc"> is what makes in-page #fragment links resolve
#: inside a sandboxed srcdoc frame, which has no base URL of its own. It is
#: RELAY-ONLY and actively harmful anywhere else: served at a real URL, that
#: base makes every relative link in the author's content resolve against
#: about:srcdoc, and the browser blocks the navigation (about:blank#blocked).
_SRCDOC_BASE = '<base href="about:srcdoc">\n'


def compose_screen(mission_id: str, pillar_id: str | None = None, *,
                   framed: bool = False, may_write: bool = True,
                   viewer: str | None = None) -> bytes | None:
    """One screen as a complete document, or None if there is nothing to serve.

    *framed* is True when this document will be handed to a sandboxed frame as
    srcdoc — the relay path. The author's content and the state block are
    identical either way; only the base differs, because the two surfaces
    genuinely differ in whether the document has a URL of its own.

    *viewer* is the participant id this screen is being rendered FOR, so the
    page can tell a question the reader asked from one asked of them. Without
    it the chrome had no way to know whose question it was showing, and so
    offered the same control for both: on your own unanswered question, the
    one that files an answer. Never trusted for authorization — every write is
    re-checked against the session cookie or the grant. It decides which
    control to draw, nothing more.

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
        + (_SRCDOC_BASE if framed else "")
        + _state_block(dict(mission_state(mission_id, pillar_id,
                                          include_sessions=not framed),
                            may_write=may_write, me=viewer))
        + "<script>\n" + bootstrap_source() + "\n</script>\n"
        + current["html"]          # byte for byte, never parsed
    )
    return document.encode("utf-8")
