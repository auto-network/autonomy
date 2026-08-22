"""Portable plugin contributions for session chrome.

Plugins own the relationship lookup and return descriptors. The dashboard
substrate owns only validation plus rendering on shared session surfaces.
"""
from __future__ import annotations

from typing import Any


SESSION_CONTRIBUTIONS_TOPIC = "session-contributions"
VALID_KINDS = {"action", "badge"}


def normalize_descriptor(
    plugin_id: str,
    session_id: str,
    raw: Any,
) -> dict[str, str] | None:
    """Return the safe, renderer-facing descriptor shape or ``None``.

    Installed plugins are trusted code (they can already ship page.js), but a
    narrow data contract prevents a typo in one plugin from breaking every
    session card. Links stay dashboard-internal and icon markup is bounded.
    """
    if not isinstance(raw, dict):
        return None
    contribution_id = str(raw.get("id") or "").strip()[:128]
    label = str(raw.get("label") or "").strip()[:80]
    kind = str(raw.get("kind") or "action").strip()
    href = str(raw.get("href") or "").strip()[:1000]
    icon_svg = str(raw.get("icon_svg") or "").strip()
    if (
        not contribution_id
        or not label
        or kind not in VALID_KINDS
        or not href.startswith("/")
        or href.startswith("//")
        or not icon_svg.startswith("<svg")
        or len(icon_svg) > 4096
    ):
        return None
    return {
        "id": f"{plugin_id}:{contribution_id}",
        "plugin_id": plugin_id,
        "session_id": session_id,
        "kind": kind,
        "label": label,
        "title": str(raw.get("title") or label).strip()[:240],
        "href": href,
        "icon_svg": icon_svg,
        "accent": str(raw.get("accent") or "").strip()[:64],
    }
