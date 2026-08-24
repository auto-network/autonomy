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
    prefix = mission_id + ":"
    out: dict[str, list] = {}
    for m in _read(CHAT_SET_ID, org):
        if not m.key.startswith(prefix):
            continue
        entries = (m.payload or {}).get("entries") or []
        if entries:
            out[m.key[len(prefix):]] = entries
    return out


def load_beads(org: str, mission_id: str, pillars: list[dict]) -> dict:
    """Task payload per pillar — the bd bridge (bridge.py)."""
    from tools.dashboard.plugins.mission import bridge
    return bridge.load_beads(mission_id, pillars)


def render_screen(org: str, mission_id: str,
                  focus_pillar_id: str | None = None) -> str | None:
    """The complete mission document, or None for an unknown mission."""
    mission = load_mission(org, mission_id)
    if mission is None:
        return None
    pillars = load_pillars(org, mission_id)
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
        "items": load_items(org, mission_id),
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
    beads = load_beads(org, mission_id, pillars)
    chat = load_chat(org, mission_id)
    inject = ""
    if beads:
        inject += ('<script type="application/json" id="mc-beads">'
                   + _blob(beads) + "</script>")
    if chat:
        inject += ('<script type="application/json" id="mc-chat">'
                   + _blob(chat) + "</script>")
    if inject:
        doc = doc.replace("<style>", inject + "<style>", 1)
    return doc
