"""Screen builder for the ``mission`` plugin.

Renders one complete, self-contained document per mission from the
plugin's own settings sets (``mission.*``), with the task payload and
chat log baked in as JSON blocks. Baking is deliberate and stays even
though v1 screens are served same-origin: it keeps the document whole
(a future relay publisher can seal it as-is), makes rendering testable
as a pure settings->document function, and spares the viewer N fetches
on open.

Reads are ``peers=[]`` throughout: mission content renders from the
owning org's database alone, never a federated view.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from tools.dashboard.plugins.mission.entrypoints.schemas import (
    CHAT_SET_ID,
    ITEM_SET_ID,
    MISSION_SET_ID,
    PILLAR_SET_ID,
)

_TEMPLATE_PATH = Path(__file__).parent / "viewer.html"
_DATA_MARK = "__MC_STRUCTURED_DATA__"


def _read(set_id: str, org: str):
    from tools.graph import ops as graph_ops
    return graph_ops.read_set(set_id, org=org or None, peers=[])


def _blob(value) -> str:
    """JSON safe for a ``<script type="application/json">`` element."""
    return json.dumps(value, ensure_ascii=False).replace("</", "<\\/")


def load_mission(org: str, mission_id: str) -> dict | None:
    for m in _read(MISSION_SET_ID, org):
        if m.key == mission_id:
            return dict(m.payload)
    return None


def load_pillars(org: str, mission_id: str) -> list[dict]:
    """The mission's pillars, orderd by their declared order then name.

    Key-prefix selection (``<mission_id>:``) — children of a parent key,
    not payload inspection.
    """
    prefix = mission_id + ":"
    out = []
    for m in _read(PILLAR_SET_ID, org):
        if not m.key.startswith(prefix):
            continue
        p = dict(m.payload)
        p["pillar_id"] = m.key[len(prefix):]
        out.append(p)
    out.sort(key=lambda p: (p.get("order") or 0.0, p.get("name") or ""))
    return out


def load_items(org: str, mission_id: str) -> list[dict]:
    """Viewer-shaped items: surface/item ids recovered from member keys.

    Blank optional fields are dropped — the viewer reapplies schema
    defaults with ``||``, and shipping them roughly doubles the JSON.
    """
    prefix = mission_id + ":"
    items: list[dict] = []
    for m in _read(ITEM_SET_ID, org):
        if not m.key.startswith(prefix):
            continue
        rest = m.key[len(prefix):]
        pillar_id, _, item_id = rest.partition(":")
        if not item_id:
            continue
        item = {
            "surface_id": pillar_id,
            "item_id": item_id,
            "key": rest,
            "created_at": m.created_at,
            "updated_at": m.updated_at,
            **{k: v for k, v in dict(m.payload).items()
               if v not in ("", [], {}, 0, 0.0, False)},
        }
        items.append(item)
    # Historical duplicates (same key, split rows from the pre-upsert
    # write path) render deterministically: newest updated_at wins.
    by_key: dict[str, dict] = {}
    for it in items:
        prev = by_key.get(it["key"])
        if prev is None or (it.get("updated_at") or "") >= \
                (prev.get("updated_at") or ""):
            by_key[it["key"]] = it
    return list(by_key.values())


def load_chat(org: str, mission_id: str) -> dict[str, list]:
    """Per-pillar logs from per-message rows (key mission:pillar:uuid)."""
    prefix = mission_id + ":"
    out: dict[str, list] = {}
    for m in _read(CHAT_SET_ID, org):
        if not m.key.startswith(prefix):
            continue
        rest = m.key[len(prefix):]
        pillar_id, _, msg_id = rest.partition(":")
        if not msg_id or not isinstance(m.payload, dict):
            continue
        out.setdefault(pillar_id, []).append(dict(m.payload))
    for entries in out.values():
        entries.sort(key=lambda e: e.get("at") or "")
    return out


def activity_summary(org: str, mission_id: str,
                     now: float | None = None) -> dict:
    """The homepage's read: when things happened and what needs eyes.

    Derived from the same streams the feed renders — item moments plus
    history/work/discussion/answer entries. Returns last_at (epoch),
    days (28 daily event counts, oldest first), blockers (open blocking
    questions), in_progress (criteria being worked), open_questions.
    """
    import datetime
    now = now or time.time()
    stamps: list[float] = []

    def _epoch(v) -> float | None:
        if not v:
            return None
        try:
            return datetime.datetime.fromisoformat(
                str(v).replace("Z", "+00:00")).timestamp()
        except Exception:
            return None

    blockers = in_progress = open_questions = 0
    previews = {"blockers": [], "in_progress": [], "open": []}
    for it in load_items(org, mission_id):
        for v in (it.get("happened_at"), it.get("updated_at")):
            e = _epoch(v)
            if e:
                stamps.append(e)
                break
        for stream, field2 in (("history", "at"), ("work", "at"),
                               ("discussion", "at")):
            for entry in it.get(stream) or []:
                e = _epoch(entry.get(field2))
                if e:
                    stamps.append(e)
        ans = it.get("answer") or {}
        e = _epoch(ans.get("at"))
        if e:
            stamps.append(e)
        kind, state = it.get("kind"), it.get("state")
        row = {"t": it.get("title") or "", "p": it.get("surface_id") or ""}
        if kind == "question" and state == "open":
            open_questions += 1
            if it.get("blocking"):
                blockers += 1
                previews["blockers"].append(row)
            else:
                previews["open"].append(row)
        if kind == "checkpoint" and state == "in_progress":
            in_progress += 1
            previews["in_progress"].append(row)

    days = [0] * 28
    for e in stamps:
        age_days = int((now - e) // 86400)
        if 0 <= age_days < 28:
            days[27 - age_days] += 1
    return {
        "last_at": max(stamps) if stamps else None,
        "days": days,
        # capped raw stamps let the homepage re-bucket to any window
        "events": sorted(stamps)[-400:],
        "previews": {k: v[:6] for k, v in previews.items()},
        "blockers": blockers,
        "in_progress": in_progress,
        "open_questions": open_questions,
    }


def load_directory(org: str) -> dict[str, dict]:
    """persona pub key -> presentation, for render-time attribution.

    Rows from the org member directory (autonomy.org.member-profile);
    while a member's row is missing, this one-human dashboard falls
    back to the personal identity's display name for its own persona —
    the org row, once written, overrides.
    """
    out: dict[str, dict] = {}
    try:
        from tools.graph import ops as graph_ops
        for m in graph_ops.read_set(
                "autonomy.org.member-profile", org=org or None, peers=[]):
            payload = m.payload or {}
            if payload.get("display_name"):
                out[m.key] = {
                    "display_name": payload["display_name"],
                    "avatar": payload.get("avatar") or "",
                    "color": payload.get("color") or "",
                }
        persona = None
        for m in graph_ops.read_set(
                "autonomy.network.persona", org=org or None, peers=[]):
            if (m.payload or {}).get("source") in ("found", "join"):
                persona = m.key
                break
        if persona and persona not in out:
            try:
                from tools.dashboard.identity_routes import _personal_member
                name = (_personal_member().payload or {}).get("display_name")
                if name:
                    out[persona] = {"display_name": name, "avatar": "",
                                    "color": ""}
            except Exception:
                pass
    except Exception:
        pass
    return out


def load_beads(org: str, mission_id: str, pillars: list[dict]) -> dict:
    """Task payload per pillar — the bd bridge (bridge.py)."""
    from tools.dashboard.plugins.mission import bridge
    return bridge.load_beads(mission_id, pillars)


def _marker(pct: int, note: str) -> str:
    """A progress heartbeat, legal ahead of the doctype (HTML comment)."""
    return f"<!--msn:{pct}|{note}-->"


def render_stages(org: str, mission_id: str,
                  focus_pillar_id: str | None = None):
    """Stage-by-stage screen composition, for a streaming response.

    Yields progress-marker comment strings while the settings loads run,
    then ``<!--msn:doc:<bytes>-->`` and finally the complete document.
    The markers are what the loading interstitial narrates; without them
    the client sees nothing until the whole compose finishes. The caller
    must have resolved the mission's existence already (``load_mission``)
    — an unknown mission raises ``KeyError`` here.
    """
    from tools.graph import ops as graph_ops
    yield _marker(3, "Counting settings")
    total = 0
    try:
        for sid in (PILLAR_SET_ID, ITEM_SET_ID, CHAT_SET_ID):
            total += graph_ops.count_set_rows(
                sid, org=org or None, prefix=mission_id)
        total += 1                                # the registry row itself
    except Exception:                             # noqa: BLE001
        total = 0                                 # narrate without counts

    got = 0

    def note(text: str) -> str:
        return (f"{text} — {got} of {total} settings" if total else text)

    yield _marker(6, note("Reading mission registry"))
    mission = load_mission(org, mission_id)
    if mission is None:
        raise KeyError(mission_id)
    got += 1
    yield _marker(10, note("Loading pillars"))
    pillars = load_pillars(org, mission_id)
    got += len(pillars)
    yield _marker(16, note("Loading mission items"))
    items = load_items(org, mission_id)
    got += len(items)
    yield _marker(42, note("Loading discussion"))
    chat = load_chat(org, mission_id)
    got += sum(len(v) for v in chat.values())
    got = min(got, total) if total else got
    yield _marker(55, note("Bridging beads"))
    beads = load_beads(org, mission_id, pillars)
    yield _marker(80, "Resolving member directory")
    directory = load_directory(org)
    yield _marker(88, "Rendering document")
    doc = _assemble(org, mission_id, focus_pillar_id, mission, pillars,
                    items, beads, chat, directory)
    yield f"<!--msn:doc:{len(doc.encode('utf-8'))}-->"
    yield doc


def render_screen(org: str, mission_id: str,
                  focus_pillar_id: str | None = None) -> str | None:
    """The complete mission document, or None for an unknown mission."""
    try:
        doc = None
        for chunk in render_stages(org, mission_id, focus_pillar_id):
            doc = chunk
        return doc
    except KeyError:
        return None


def _assemble(org: str, mission_id: str, focus_pillar_id: str | None,
              mission: dict, pillars: list[dict], items: list[dict],
              beads: dict, chat: dict, directory: dict) -> str:
    data = {
        "generated_at": time.time(),
        "focus": focus_pillar_id or "",
        "mission": {
            "mission_id": mission_id,
            "name": mission.get("name", ""),
            "status": mission.get("status", "active"),
            "org": org,
        },
        "pillars": [
            {
                "pillar_id": p["pillar_id"],
                "name": p.get("name", ""),
                "color": p.get("color") or "",
                "status": p.get("status", "active"),
            }
            for p in pillars
        ],
        "items": items,
    }
    # The template is the content layer; this shell is what makes it a
    # phone-correct document. Without the viewport meta, mobile browsers
    # lay out at ~980px and shrink — everything renders tiny.
    doc = ('<!doctype html><html><head><meta charset="utf-8">'
           '<meta name="viewport" '
           'content="width=device-width, initial-scale=1">'
           '<style>html{overflow-x:hidden}'
           'body{margin:0;background:#0c0f14}</style></head><body>'
           + _TEMPLATE_PATH.read_text(encoding="utf-8").replace(
               _DATA_MARK, _blob(data))
           + "</body></html>")
    inject = ""
    if directory:
        inject += ('<script type="application/json" id="mc-directory">'
                   + _blob(directory) + "</script>")
    if beads:
        inject += ('<script type="application/json" id="mc-beads">'
                   + _blob(beads) + "</script>")
    if chat:
        inject += ('<script type="application/json" id="mc-chat">'
                   + _blob(chat) + "</script>")
    if inject:
        doc = doc.replace("<style>", inject + "<style>", 1)
    return doc
