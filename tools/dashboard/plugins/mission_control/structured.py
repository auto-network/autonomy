"""Structured-style screen builder.

A ``structured`` mission's screens are not coordinator-pushed HTML. The
content lives as ``dashboard.mission.item`` Settings rows (see
``entrypoints/schemas.py``); this module resolves those rows for the
mission's org and bakes them into the platform's standard viewer template
as a JSON block. The template's inline script renders everything
client-side — one app for the whole mission, every pillar included.

Why baked rather than fetched: a visitor Content Frame has no origin and
no dashboard credential (screen-contract rule 6), so the document must
carry its own data. The platform chrome (state block + bootstrap) is
prepended by ``compose.compose_screen`` exactly as for free-form screens
— this module only replaces the author-HTML half.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from tools.dashboard.plugins.mission_control.entrypoints.schemas import (
    MISSION_ITEM_SET_ID,
)

_TEMPLATE_PATH = Path(__file__).parent / "structured_viewer.html"

#: Substitution marker in the template. The JSON is placed inside a
#: ``<script type="application/json">`` element, so ``</`` must be escaped
#: to keep a body containing "</script>" from terminating the block.
_DATA_MARK = "__MC_STRUCTURED_DATA__"


def _template_source() -> str:
    return _TEMPLATE_PATH.read_text(encoding="utf-8")


def load_items(org: str, surface_ids: list[str]) -> list[dict]:
    """Resolve every mission item for *surface_ids* from *org*'s store.

    One ``read_set`` for the whole mission — the set is keyed
    ``<surface_id>:<item_id>``, so membership in this mission is a payload
    filter, not N queries. Rows whose payload fails to carry a known
    surface are dropped rather than mis-rendered.
    """
    from tools.graph import ops as graph_ops

    wanted = set(surface_ids)
    members = graph_ops.read_set(MISSION_ITEM_SET_ID, org=org or None, peers=[])
    items: list[dict] = []
    for m in members:
        payload = m.payload if isinstance(m.payload, dict) else {}
        if payload.get("surface_id") not in wanted:
            continue
        items.append({
            "key": m.key,
            "created_at": m.created_at,
            "updated_at": m.updated_at,
            **payload,
        })
    return items


def render_screen(mission: dict, pillars: list[dict],
                  focus_pillar_id: str | None = None) -> str:
    """The author-HTML half of a structured screen, as a string.

    *focus_pillar_id* records which surface the reader navigated to; the
    app opens on that pillar's view. The document is identical otherwise,
    which keeps the mission URL the only link anyone needs to hand out.
    """
    mission_id = mission["mission_id"]
    surface_ids = [mission_id] + [p["pillar_id"] for p in pillars]
    data = {
        "generated_at": time.time(),
        "focus": focus_pillar_id or "",
        "mission": {
            "mission_id": mission_id,
            "name": mission.get("name", ""),
            "status": mission.get("status", "active"),
            "org": mission.get("org", ""),
        },
        "pillars": [
            {
                "pillar_id": p["pillar_id"],
                "name": p.get("name", ""),
                "color": p.get("color") or "",
                "status": p.get("status", "active"),
                "last_done": p.get("last_done") or "",
                "last_done_at": p.get("last_done_at"),
            }
            for p in pillars
        ],
        "items": load_items(mission.get("org") or "", surface_ids),
    }
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return _template_source().replace(_DATA_MARK, blob)
