"""``graph mission`` — drive a structured Mission Control mission from the CLI.

Every verb is one authenticated dashboard API call (session bearer +
``GRAPH_API``); coordinators never need curl or the settings substrate.
Surfaces are named loosely: a mission or pillar id, an id prefix, or a
case-insensitive name substring all resolve, so ``graph mission state
relay work:x proven`` works from any session.

Multiline input: ``--body -`` (and ``--evidence -``) read stdin, so a
coordinator can pipe a paragraph in without shell-quoting it.
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.error
import urllib.request


ITEM_KINDS = ("scope", "work", "checkpoint", "decision", "question",
              "status", "metric", "incident", "exhibit")
ITEM_STATES = ("proven", "done", "settled", "active", "code_only", "next",
               "specified", "open", "blocked", "deferred", "retired")

_STATE_GLYPH = {
    "proven": "✓", "done": "✓", "settled": "✓",
    "active": "●", "code_only": "◐", "open": "?",
    "blocked": "✗", "next": "→", "specified": "▢",
    "deferred": "…", "retired": "·",
}


def _api():
    from tools.graph.cli import _resolve_crosstalk_token
    base = os.environ.get("GRAPH_API", "https://localhost:8080")
    token = _resolve_crosstalk_token()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    def call(method: str, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(
            base + path,
            data=None if body is None else json.dumps(body).encode(),
            method=method,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {token}"},
        )
        try:
            return json.loads(urllib.request.urlopen(
                req, timeout=60, context=ctx).read())
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read()).get("error", str(exc))
            except Exception:
                detail = str(exc)
            print(f"mission: {method} {path}: {detail}", file=sys.stderr)
            sys.exit(1)
        except urllib.error.URLError as exc:
            print(f"mission: cannot reach dashboard: {exc.reason}",
                  file=sys.stderr)
            sys.exit(1)

    return call


def _resolve_surface(call, ref: str) -> tuple[str, str, dict]:
    """Resolve *ref* to ('missions'|'pillars', surface_id, mission_row).

    Matching order: exact id, id prefix, then case-insensitive name
    substring — across every mission and its pillars. Ambiguity is an
    error that lists the candidates rather than a guess.
    """
    ref_l = ref.lower()
    missions = call("GET", "/api/missions").get("missions", [])
    candidates = []
    for m in missions:
        pillars = call("GET", f"/api/missions/{m['mission_id']}/pillars"
                       ).get("pillars", [])
        for kind, sid, name in (
            [("missions", m["mission_id"], m["name"])]
            + [("pillars", p["pillar_id"], p["name"]) for p in pillars]
        ):
            if sid == ref:
                return kind, sid, m
            if sid.startswith(ref) or ref_l in name.lower():
                candidates.append((kind, sid, m, name))
    if len(candidates) == 1:
        kind, sid, m, _ = candidates[0]
        return kind, sid, m
    if not candidates:
        print(f"mission: no mission or pillar matches {ref!r}", file=sys.stderr)
    else:
        print(f"mission: {ref!r} is ambiguous:", file=sys.stderr)
        for kind, sid, m, name in candidates:
            where = "" if kind == "missions" else f"  (in {m['name']})"
            print(f"  {sid[:12]}  {name}{where}", file=sys.stderr)
    sys.exit(1)


def _read_maybe_stdin(value: str | None) -> str | None:
    if value == "-":
        return sys.stdin.read()
    return value


def _item_flags_to_payload(args) -> dict:
    """The optional item fields shared by ``add`` and ``update``."""
    payload: dict = {}
    for flag, key in (("title", "title"), ("section", "section"),
                      ("ask", "ask"), ("fork", "fork"), ("chosen", "chosen"),
                      ("if_wrong", "if_wrong"), ("value", "value"),
                      ("owner", "owner"), ("happened_at", "happened_at")):
        v = getattr(args, flag, None)
        if v is not None:
            payload[key] = v
    if getattr(args, "body", None) is not None:
        payload["body"] = _read_maybe_stdin(args.body) or ""
    if getattr(args, "state", None) is not None:
        payload["state"] = args.state
    if getattr(args, "kind", None) is not None:
        payload["kind"] = args.kind
    if getattr(args, "order", None) is not None:
        payload["order"] = args.order
    if getattr(args, "evidence", None):
        ev = args.evidence
        if ev == ["-"]:
            ev = [ln.strip() for ln in sys.stdin.read().splitlines()
                  if ln.strip()]
        payload["evidence"] = ev
    if getattr(args, "ref", None):
        payload["refs"] = args.ref
    return payload


# ── verbs ─────────────────────────────────────────────────────────


def cmd_mission_list(args):
    call = _api()
    missions = call("GET", "/api/missions").get("missions", [])
    if not missions:
        print("no missions")
        return
    for m in missions:
        print(f"{m['mission_id'][:12]}  {m.get('style', 'freeform'):10s} "
              f"{m['status']:8s} {m.get('org', ''):10s} {m['name']}")


def cmd_mission_status(args):
    call = _api()
    kind, sid, mission = _resolve_surface(call, args.surface)
    mid = mission["mission_id"]
    pillars = call("GET", f"/api/missions/{mid}/pillars").get("pillars", [])
    items = call("GET", f"/api/missions/{mid}/items").get("items", [])
    live = [i for i in items if i.get("state") != "retired"]
    print(f"{mission['name']}  [{mission.get('style', 'freeform')}, "
          f"{mission['status']}]  {len(live)} items")
    surfaces = [(mid, "Overview")] + [(p["pillar_id"], p["name"])
                                      for p in pillars]
    for surf_id, name in surfaces:
        mine = [i for i in live if i.get("surface_id") == surf_id]
        if kind == "pillars" and surf_id != sid:
            continue
        counts: dict[str, int] = {}
        for i in mine:
            counts[i["state"]] = counts.get(i["state"], 0) + 1
        summary = "  ".join(f"{n} {s}" for s, n in sorted(
            counts.items(), key=lambda kv: -kv[1]))
        asks = sum(1 for i in mine if i.get("kind") == "question"
                   and i.get("state") == "open" and i.get("ask"))
        ask_note = f"  ⚑{asks} ask{'s' if asks != 1 else ''}" if asks else ""
        print(f"  {name:38.38s} {len(mine):3d} items  {summary}{ask_note}")
        if kind == "pillars" or args.verbose:
            for i in sorted(mine, key=lambda x: (x.get("kind", ""),
                                                 x.get("order", 0))):
                g = _STATE_GLYPH.get(i["state"], "·")
                print(f"    {g} [{i['kind']:10s}] {i['item_id']:32.32s} "
                      f"{i['title']}")


def cmd_mission_items(args):
    call = _api()
    kind, sid, mission = _resolve_surface(call, args.surface)
    items = call("GET", f"/api/{kind}/{sid}/items").get("items", [])
    if kind == "missions":
        items = [i for i in items if args.all_surfaces
                 or i.get("surface_id") == sid]
    if args.kind:
        items = [i for i in items if i.get("kind") == args.kind]
    if args.state:
        items = [i for i in items if i.get("state") == args.state]
    if args.json_output:
        print(json.dumps(items, indent=2))
        return
    for i in sorted(items, key=lambda x: (x.get("section", ""),
                                          x.get("order", 0))):
        g = _STATE_GLYPH.get(i.get("state", ""), "·")
        print(f"{g} {i.get('state', ''):10s} [{i.get('kind', ''):10s}] "
              f"{i.get('item_id', ''):36.36s} {i.get('title', '')}")


def cmd_mission_add(args):
    call = _api()
    kind, sid, _ = _resolve_surface(call, args.surface)
    payload = _item_flags_to_payload(args)
    if "kind" not in payload:
        print("mission add: --kind is required", file=sys.stderr)
        sys.exit(2)
    if "title" not in payload:
        print("mission add: --title is required", file=sys.stderr)
        sys.exit(2)
    out = call("PUT", f"/api/{kind}/{sid}/items/{args.item_id}", payload)
    print(f"  ✓ {out['key']}  [{payload['kind']}] {payload['title']}")


def cmd_mission_update(args):
    call = _api()
    kind, sid, _ = _resolve_surface(call, args.surface)
    items = call("GET", f"/api/{kind}/{sid}/items").get("items", [])
    current = next((i for i in items if i.get("item_id") == args.item_id
                    and i.get("surface_id") == sid), None)
    if current is None:
        print(f"mission update: no item {args.item_id!r} on that surface",
              file=sys.stderr)
        sys.exit(1)
    payload = {k: v for k, v in current.items()
               if k not in ("key", "created_at", "updated_at")}
    payload.update(_item_flags_to_payload(args))
    out = call("PUT", f"/api/{kind}/{sid}/items/{args.item_id}", payload)
    print(f"  ✓ {out['key']} updated")


def cmd_mission_state(args):
    call = _api()
    kind, sid, _ = _resolve_surface(call, args.surface)
    body = {"state": args.state}
    if args.note:
        body["note"] = _read_maybe_stdin(args.note)
    if args.happened_at:
        body["happened_at"] = args.happened_at
    out = call("POST", f"/api/{kind}/{sid}/items/{args.item_id}/state", body)
    print(f"  ✓ {out['key']} → {args.state}")


def cmd_mission_retire(args):
    args.state = "retired"
    args.happened_at = None
    cmd_mission_state(args)


def cmd_mission_coverage(args):
    """Beads labeled for the mission that no acceptance criterion covers.

    Coverage means some checkpoint item carries a ``bead:<id>`` ref. The
    bead pool is ``bd list --label mission:<mission_id> --all`` (closed
    beads count: completed work is delivery evidence). The ladder column
    is derived, never stored: closed=complete, in_progress=running
    (approval-for-dispatch is the true bar; bd status is the visible
    proxy), design/acceptance filled=specified, else defined.
    """
    import subprocess

    call = _api()
    kind, sid, mission = _resolve_surface(call, args.surface)
    mid = mission["mission_id"]
    items = call("GET", f"/api/missions/{mid}/items").get("items", [])
    covered = set()
    checkpoints = 0
    for it in items:
        if it.get("kind") != "checkpoint":
            continue
        checkpoints += 1
        for ref in it.get("refs") or []:
            if str(ref).startswith("bead:"):
                covered.add(str(ref)[5:])
    try:
        raw = subprocess.run(
            ["bd", "list", "--label", f"mission:{mid}", "--all", "--json"],
            capture_output=True, text=True, timeout=60).stdout
        beads = json.loads(raw) if raw.strip() else []
    except Exception as exc:
        print(f"mission coverage: bd query failed: {exc}", file=sys.stderr)
        sys.exit(1)

    def ladder(row) -> str:
        if row.get("status") == "closed":
            return "complete"
        if row.get("status") == "in_progress":
            return "running"
        try:
            show = subprocess.run(["bd", "show", row["id"], "--json"],
                                  capture_output=True, text=True,
                                  timeout=30).stdout
            full = json.loads(show)
            full = full[0] if isinstance(full, list) else full
        except Exception:
            full = row
        if (full.get("design") or "").strip() or \
           (full.get("acceptance_criteria") or "").strip():
            return "specified"
        return "defined"

    def pillar_of(row) -> str:
        for lb in row.get("labels") or []:
            if lb.startswith("pillar:"):
                return lb[7:]
        return "(no pillar)"

    uncovered = [b for b in beads if b["id"] not in covered]
    print(f"{mission['name']}: {len(beads)} beads on the mission, "
          f"{checkpoints} criteria covering {len(covered)}, "
          f"{len(uncovered)} uncovered")
    by_pillar: dict[str, list] = {}
    for b in uncovered:
        by_pillar.setdefault(pillar_of(b), []).append(b)
    for pname in sorted(by_pillar):
        print(f"  {pname}")
        for b in by_pillar[pname]:
            mark = "epic " if b.get("issue_type") == "epic" else ""
            print(f"    {b['id']:14s} {ladder(b):9s} {mark}{b['title']}")
    stale = sorted(covered - {b["id"] for b in beads})
    if stale:
        print("  criteria referencing beads NOT labeled for the mission:")
        for bid in stale:
            print(f"    {bid}")


def cmd_mission_style(args):
    call = _api()
    kind, sid, mission = _resolve_surface(call, args.surface)
    out = call("POST", f"/api/missions/{mission['mission_id']}/style",
               {"style": args.style})
    m = out["mission"]
    print(f"  ✓ {m['name']} → {m['style']}")


def register(sub) -> None:
    """Attach the ``mission`` command tree to the graph CLI's subparsers."""
    p = sub.add_parser(
        "mission",
        help="Drive a structured Mission Control mission (status, items, "
             "transitions) without curl",
    )
    p.set_defaults(func=lambda _a: p.print_help())
    ms = p.add_subparsers(dest="mission_subcmd")

    q = ms.add_parser("list", help="All missions with style and status")
    q.set_defaults(func=cmd_mission_list)

    q = ms.add_parser("status", help="Mission/pillar report: item counts by "
                                     "state, open asks, per-item detail")
    q.add_argument("surface", help="Mission or pillar (id, prefix, or name)")
    q.add_argument("-v", "--verbose", action="store_true",
                   help="Per-item lines for every surface")
    q.set_defaults(func=cmd_mission_status)

    q = ms.add_parser("items", help="List items on a surface")
    q.add_argument("surface")
    q.add_argument("--kind", choices=ITEM_KINDS)
    q.add_argument("--state", choices=ITEM_STATES)
    q.add_argument("--all-surfaces", action="store_true",
                   help="On a mission: include every pillar's items")
    q.add_argument("--json", dest="json_output", action="store_true")
    q.set_defaults(func=cmd_mission_items)

    def item_flags(qq, require_kind: bool):
        qq.add_argument("surface")
        qq.add_argument("item_id")
        qq.add_argument("--kind", choices=ITEM_KINDS,
                        required=False)
        qq.add_argument("--title")
        qq.add_argument("--body", help="Item prose; '-' reads stdin")
        qq.add_argument("--state", choices=ITEM_STATES)
        qq.add_argument("--section")
        qq.add_argument("--order", type=float)
        qq.add_argument("--evidence", action="append",
                        help="Repeatable; a single '-' reads one per stdin line")
        qq.add_argument("--ref", action="append",
                        help="Repeatable: commit:<sha> bead:<id> graph:<id>")
        qq.add_argument("--ask", help="Operator ask text (question kind)")
        qq.add_argument("--fork")
        qq.add_argument("--chosen")
        qq.add_argument("--if-wrong", dest="if_wrong")
        qq.add_argument("--value", help="Metric figure, units included")
        qq.add_argument("--owner")
        qq.add_argument("--happened-at", dest="happened_at",
                        help="ISO-8601 moment the state was earned")

    q = ms.add_parser("add", help="Create (or fully rewrite) one item")
    item_flags(q, require_kind=True)
    q.set_defaults(func=cmd_mission_add)

    q = ms.add_parser("update", help="Patch fields on an existing item")
    item_flags(q, require_kind=False)
    q.set_defaults(func=cmd_mission_update)

    q = ms.add_parser("state", help="Transition an item's state")
    q.add_argument("surface")
    q.add_argument("item_id")
    q.add_argument("state", choices=ITEM_STATES)
    q.add_argument("--note", help="Appended to the item body; '-' reads stdin")
    q.add_argument("--happened-at", dest="happened_at")
    q.set_defaults(func=cmd_mission_state)

    q = ms.add_parser("retire", help="Retire an item (kept in the record, "
                                     "hidden from the screen)")
    q.add_argument("surface")
    q.add_argument("item_id")
    q.add_argument("--note")
    q.set_defaults(func=cmd_mission_retire)

    q = ms.add_parser("coverage", help="Beads on the mission that no "
                                       "acceptance criterion covers")
    q.add_argument("surface", help="The mission (id, prefix, or name)")
    q.set_defaults(func=cmd_mission_coverage)

    q = ms.add_parser("style", help="Switch a mission between freeform and "
                                    "structured rendering")
    q.add_argument("surface", help="The mission (id, prefix, or name)")
    q.add_argument("style", choices=["freeform", "structured"])
    q.set_defaults(func=cmd_mission_style)
