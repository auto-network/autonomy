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
* an epic (``issue_type: epic``) is the pillar's final-acceptance task.

Two payloads, two costs. The SCREEN carries only what its lists and
arcs read — id, title, ladder state, dependency ids, the epic flag and
a comment count — from one ``bd list`` and one typed dependency batch.
The list command no longer includes dependency arrays. Everything a reader opens a task to see — the
description, a closed bead's close reason, the bd comments — is the
DETAIL, fetched per task from ``/api/mission/tasks/<m>/detail`` when a
sheet opens. Before this split the render ran ``bd show`` over every
id (linear, and each dependency inlined whole) plus one ``bd comments``
subprocess per commented bead: seven to nine seconds and 300KB baked
into the document for a 158-bead mission, almost none of it ever read.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess

from tools.data_paths import beads_client_env, org_beads_dir

_TIMEOUT_S = 30
_DESC_LIMIT = 8000
_log = logging.getLogger(__name__)


def _beads_env(org: str | None) -> dict:
    """Route bd to the mission org's tracker (autonomy@74585ba).

    An org with a provisioned tracker dir gets BEADS_DIR (and that
    tracker's SQL credentials) pointed at it — the same config the
    launcher mounts as that org's /data/.beads. Everyone else gets the
    ambient tracker PLUS the shared tracker's credentials: the dolt
    server requires per-org SQL auth for every caller, and the
    dashboard's own process env carries none.
    """
    return {**os.environ, **beads_client_env(org_beads_dir(org))}


def _bd(args: list[str], org: str | None = None) -> list | dict | None:
    try:
        result = subprocess.run(
            ["bd", *args, "--json"], capture_output=True, text=True,
            timeout=_TIMEOUT_S, env=_beads_env(org))
        if result.returncode:
            return None
        raw = result.stdout
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


def _pillar_of(row: dict, label_map: dict[str, str]) -> str | None:
    return next((label_map[lb] for lb in row.get("labels") or []
                 if lb in label_map), None)


def group_dependencies(records: object) -> dict[str, list[dict]] | None:
    """Validate a whole flat batch; partial edges must never imply readiness.

    Keep unknown edge types inspectable, but strip unused edge metadata.
    ``None`` is unavailable; ``{}`` is a successfully read empty graph.
    """
    if not isinstance(records, list):
        return None
    grouped: dict[str, list[dict]] = {}
    for edge in records:
        if not isinstance(edge, dict) or any(
            not isinstance(edge.get(k), str) or not edge[k].strip()
            or edge[k] != edge[k].strip()
            for k in ("issue_id", "depends_on_id", "type")
        ):
            return None
        clean = {k: edge[k] for k in ("issue_id", "depends_on_id", "type")}
        grouped.setdefault(edge["issue_id"], []).append(clean)
    return grouped


def _summary(row: dict, edges: list[dict] | None = None,
             *, dependencies_known: bool = False) -> dict:
    """The screen's view of one bead — what lists, arcs and ranks read."""
    task = {
        "id": row["id"],
        "title": row.get("title") or "",
        "state": _ladder(row),
        "dependencies": edges or [],
        "dependencies_known": dependencies_known,
        "deps": sorted({d["depends_on_id"] for d in edges or []}),
        "blocks_on": sorted({d["depends_on_id"] for d in edges or []
                             if d["type"] == "blocks"}),
    }
    parents = sorted({d["depends_on_id"] for d in edges or []
                      if d["type"] == "parent-child"})
    if parents:
        task["parent"] = parents[0]
    if row.get("issue_type") == "epic":
        task["epic"] = True
    if row.get("comment_count"):
        task["comment_count"] = int(row["comment_count"])
    return task


def load_beads(mission_id: str, pillars: list[dict],
               org: str | None = None) -> dict[str, list[dict]]:
    """``{pillar_id: [task, ...]}`` for every pillar with mapped beads.

    One list and one dependency batch, never per-bead follow-ups.
    """
    label_map: dict[str, str] = {}
    for p in pillars:
        for lb in p.get("bead_labels") or []:
            label_map[lb] = p["pillar_id"]
    if not label_map:
        return {}
    # --all currently overrides the default limit; pin it explicitly too.
    rows = _bd(["list", "--label", f"mission:{mission_id}", "--all",
                "--limit", "0"],
               org=org)
    if not isinstance(rows, list) or not rows:
        return {}
    rows = [r for r in rows if isinstance(r, dict) and r.get("id")]
    if not rows:
        return {}
    edges = group_dependencies(_bd(["dep", "list", *[r["id"] for r in rows]],
                                   org=org))
    if edges is None:
        _log.warning("Mission task dependencies unavailable; rendering unknown edges")
    out: dict[str, list[dict]] = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        pillar_id = _pillar_of(row, label_map)
        if pillar_id is None:
            continue
        out.setdefault(pillar_id, []).append(_summary(
            row, (edges or {}).get(row["id"], []),
            dependencies_known=edges is not None))
    return out


#: Detail requests are bounded: a sheet opens one task, a criterion page
#: a handful of linked beads. Anything larger is a screen, not a detail.
DETAIL_LIMIT = 40


def load_task_detail(mission_id: str, bead_ids: list[str],
                     org: str | None = None) -> dict[str, dict]:
    """``{bead_id: detail}`` for the beads a reader opened.

    Membership is re-checked against the mission label so the route
    cannot be used to read arbitrary beads through a mission it names.
    One ``bd show`` for the batch, then ``bd comments`` — concurrently —
    for the beads that actually have comments.
    """
    ids = []
    for bid in bead_ids:
        bid = (bid or "").strip()
        if bid and bid not in ids:
            ids.append(bid)
    ids = ids[:DETAIL_LIMIT]
    if not ids:
        return {}
    rows = _bd(["show", *ids], org=org)
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list):
        return {}
    wanted = f"mission:{mission_id}"
    rows = [r for r in rows if isinstance(r, dict) and r.get("id") in ids
            and wanted in (r.get("labels") or [])]

    def _comments(bid: str) -> list[dict]:
        got = _bd(["comments", bid], org=org) or []
        return [
            {"by": c.get("author") or "", "at": c.get("created_at") or "",
             "text": c.get("text") or ""}
            for c in got if isinstance(c, dict)]

    commented = [r["id"] for r in rows if r.get("comment_count")]
    comments: dict[str, list[dict]] = {}
    if commented:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(8, len(commented))) as ex:
            for bid, got in zip(commented, ex.map(_comments, commented)):
                comments[bid] = got
    out: dict[str, dict] = {}
    for r in rows:
        # Some bd show versions still inline dependencies. Preserve that
        # legacy detail contract without adding a per-detail subprocess.
        inline = r.get("dependencies")
        converted = None
        if isinstance(inline, list):
            converted = group_dependencies([
                {"issue_id": r["id"], "depends_on_id": d.get("id"),
                 "type": d.get("dependency_type") or d.get("type") or "unknown"}
                if isinstance(d, dict) else d for d in inline])
        detail = _summary(r, (converted or {}).get(r["id"], []),
                          dependencies_known=converted is not None)
        detail["desc"] = _trim(r.get("description") or "")
        detail["evidence"] = ((r.get("close_reason") or "").strip()
                              if r.get("status") == "closed" else "")
        detail["comments"] = comments.get(r["id"], [])
        out[r["id"]] = detail
    return out
