"""The bead bridge: the task payload, read from bd at render time.

Tasks are beads, always — never duplicated into Settings. This module
derives the per-pillar task lists the viewer bakes in:

* membership — beads labeled ``mission:<mission_id>`` (closed included:
  completed work is delivery evidence);
* ownership — the bead's ``pillar:*`` label, mapped through each
  pillar's declared ``bead_labels`` (the organic bd vocabulary never
  needs a mass retag);
* the ladder, derived and never stored: ``closed`` → complete,
  ``in_progress`` → running (the visible proxy for approved-for-
  dispatch), design or acceptance criteria filled → specified, else
  defined;
* an epic (``issue_type: epic``) is the pillar's final-acceptance task;
* the bead's conversation — bd comments — rides along, so chat-born
  clarifications recorded with ``bd comment`` render on the task page.

Two batched ``bd`` invocations per render (list, then show for the
detail fields the list omits), plus one comments call per bead that
actually has comments.
"""
from __future__ import annotations

import json
import os
import subprocess

from tools.data_paths import beads_client_env, org_beads_dir

_TIMEOUT_S = 30
_DESC_LIMIT = 1400


def _beads_env(org: str | None) -> dict | None:
    """Route bd to the mission org's tracker (autonomy@74585ba).

    An org with a provisioned tracker dir gets BEADS_DIR (and that
    tracker's SQL credentials) pointed at it — the same config the
    launcher mounts as that org's /data/.beads — so an anchore
    mission's ``mission:<uuid>`` beads resolve from the anchore
    database. No org dir → inherit the ambient (shared) tracker.
    """
    d = org_beads_dir(org)
    if d is not None:
        return {**os.environ, **beads_client_env(d)}
    return None


def _bd(args: list[str], org: str | None = None) -> list | dict | None:
    try:
        raw = subprocess.run(
            ["bd", *args, "--json"], capture_output=True, text=True,
            timeout=_TIMEOUT_S, env=_beads_env(org)).stdout
        return json.loads(raw) if raw.strip() else None
    except Exception:
        return None


def _ladder(row: dict) -> str:
    if row.get("status") == "closed":
        return "complete"
    if row.get("status") == "in_progress":
        return "running"
    if (row.get("design") or "").strip() or \
       (row.get("acceptance_criteria") or "").strip():
        return "specified"
    return "defined"


def _trim(text: str) -> str:
    text = (text or "").strip()
    if len(text) <= _DESC_LIMIT:
        return text
    return text[:_DESC_LIMIT].rsplit("\n", 1)[0] + "\n…"


def load_beads(mission_id: str, pillars: list[dict],
               org: str | None = None) -> dict[str, list[dict]]:
    """``{pillar_id: [task, ...]}`` for every pillar with mapped beads."""
    label_map: dict[str, str] = {}
    for p in pillars:
        for lb in p.get("bead_labels") or []:
            label_map[lb] = p["pillar_id"]
    if not label_map:
        return {}
    rows = _bd(["list", "--label", f"mission:{mission_id}", "--all"],
               org=org)
    if not isinstance(rows, list) or not rows:
        return {}
    ids = [r["id"] for r in rows if r.get("id")]
    full_rows = _bd(["show", *ids], org=org) or []
    if isinstance(full_rows, dict):
        full_rows = [full_rows]
    full = {r["id"]: r for r in full_rows if isinstance(r, dict) and r.get("id")}

    out: dict[str, list[dict]] = {}
    for row in rows:
        detail = full.get(row.get("id")) or row
        pillar_id = next(
            (label_map[lb] for lb in detail.get("labels") or []
             if lb in label_map), None)
        if pillar_id is None:
            continue
        task = {
            "id": detail["id"],
            "title": detail.get("title") or "",
            "state": _ladder(detail),
            "desc": _trim(detail.get("description") or ""),
            "evidence": (detail.get("close_reason") or "").strip()
                        if detail.get("status") == "closed" else "",
            "deps": [d.get("id") for d in detail.get("dependencies") or []
                     if isinstance(d, dict) and d.get("id")],
        }
        if detail.get("issue_type") == "epic":
            task["epic"] = True
        if detail.get("comment_count"):
            comments = _bd(["comments", detail["id"]], org=org) or []
            task["comments"] = [
                {"by": c.get("author") or "", "at": c.get("created_at") or "",
                 "text": c.get("text") or ""}
                for c in comments if isinstance(c, dict)]
        out.setdefault(pillar_id, []).append(task)
    return out
