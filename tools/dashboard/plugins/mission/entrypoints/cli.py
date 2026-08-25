"""``graph mission`` — the mission plugin's command tree.

Mounted dynamically by the substrate (``entrypoints.cli`` in
plugin.yaml) while the plugin is enabled; at cutover this replaces the
legacy static registration, so the verb has exactly one owner.

Every verb is one authenticated dashboard API call against the plugin's
own routes — sessions hold no org database, so HTTP is the only honest
transport. The verbs are the Mission Control vocabulary the skill
teaches: read your pillar, work the criteria, transition with
provenance, reply/progress/answer questions, post news, record
decisions, chat, and audit coverage.

Kept import-light on purpose: this module loads on every ``graph``
invocation once the plugin is enabled.
"""
from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.error
import urllib.request

MARKS = {"confirmed": "✓", "in_progress": "⟳", "pending": "○",
         "open": "?", "answered": "✓",
         "complete": "✓", "running": "⟳", "specified": "○",
         "defined": "◦"}


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


def _stdin(value: str | None) -> str | None:
    if value == "-":
        return sys.stdin.read().strip()
    return value


def _resolve_mission(call, ref: str) -> dict:
    """id, id prefix, or case-insensitive name substring — one mission."""
    missions = call("GET", "/api/mission/missions").get("missions", [])
    exact = [m for m in missions if m["mission_id"] == ref]
    if exact:
        return exact[0]
    ref_l = ref.lower()
    hits = [m for m in missions
            if m["mission_id"].startswith(ref) or ref_l in m["name"].lower()]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        print(f"mission: nothing matches {ref!r}", file=sys.stderr)
    else:
        print(f"mission: {ref!r} is ambiguous:", file=sys.stderr)
        for m in hits:
            print(f"  {m['mission_id'][:12]}  {m['name']}", file=sys.stderr)
    sys.exit(1)


# ── verbs ─────────────────────────────────────────────────────────


def cmd_list(args):
    call = _api()
    missions = call("GET", "/api/mission/missions").get("missions", [])
    if not missions:
        print("no missions")
        return
    for m in missions:
        print(f"{m['mission_id'][:12]}  {m.get('status', ''):8s} {m['name']}")


def cmd_status(args):
    """The pillar-readable state of the world: delivery ladder, blockers,
    task ladder — the same derivations the screens show."""
    call = _api()
    m = _resolve_mission(call, args.mission)
    mid = m["mission_id"]
    pillars = call("GET", f"/api/mission/pillars/{mid}").get("pillars", [])
    items = call("GET", f"/api/mission/items/{mid}").get("items", [])
    tasks = call("GET", f"/api/mission/tasks/{mid}").get("tasks", {})
    print(f"{m['name']}  [{m.get('status', 'active')}]")
    for p in pillars:
        if args.pillar and args.pillar != p["pillar_id"]:
            continue
        pid = p["pillar_id"]
        mine = [i for i in items
                if i["surface_id"] == pid and not i.get("retired")]
        crits = [i for i in mine if i.get("kind") == "checkpoint"]
        qs = [i for i in mine if i.get("kind") == "question"]
        parts = []
        if crits:
            ok = sum(1 for c in crits if c.get("state") == "confirmed")
            run = sum(1 for c in crits if c.get("state") == "in_progress")
            parts.append(f"delivery {ok}/{len(crits)}"
                         + (f" ({run} in progress)" if run else ""))
        tl = tasks.get(pid) or []
        if tl:
            done = sum(1 for t in tl if t["state"] == "complete")
            running = sum(1 for t in tl if t["state"] == "running")
            parts.append(f"tasks {done}/{len(tl)}"
                         + (f" ({running} running)" if running else ""))
        blocking = [q for q in qs
                    if q.get("state") == "open" and q.get("blocking")]
        open_q = [q for q in qs if q.get("state") == "open"]
        if open_q:
            parts.append(f"{len(open_q)} open question"
                         + ("s" if len(open_q) != 1 else ""))
        for q in blocking:
            parts.append(f"BLOCKED on: {q.get('title', '')}")
        print(f"  {p.get('name', pid):40.40s} " + " · ".join(parts))
        if args.verbose or args.pillar:
            for c in crits:
                print(f"    {MARKS.get(c.get('state'), '○')} "
                      f"{c.get('item_id', ''):28.28s} {c.get('title', '')}")
            for t in tl:
                print(f"    {MARKS.get(t['state'], '◦')} "
                      f"{t['id']:28.28s} {t['title']}")


def cmd_items(args):
    call = _api()
    m = _resolve_mission(call, args.mission)
    path = f"/api/mission/items/{m['mission_id']}"
    if args.pillar:
        path += f"?pillar={args.pillar}"
    items = call("GET", path).get("items", [])
    if args.kind:
        items = [i for i in items if i.get("kind") == args.kind]
    if args.json_output:
        print(json.dumps(items, indent=2))
        return
    for i in items:
        g = MARKS.get(i.get("state"), "·")
        print(f"{g} [{i.get('kind', ''):10s}] "
              f"{i['surface_id']}:{i['item_id']:26.26s} {i.get('title', '')}")


def _payload_from_flags(args) -> dict:
    payload: dict = {}
    if getattr(args, "from_file", None):
        with open(args.from_file) as fh:
            payload.update(json.load(fh))
    for flag in ("kind", "title", "section", "ask", "fork", "chosen",
                 "if_wrong", "owner", "asked_by", "asked_at", "happened_at"):
        v = getattr(args, flag, None)
        if v is not None:
            payload[flag] = v
    body = _stdin(getattr(args, "body", None))
    if body is not None:
        payload["body"] = body
    if getattr(args, "state", None) is not None:
        payload["state"] = args.state
    if getattr(args, "order", None) is not None:
        payload["order"] = args.order
    if getattr(args, "blocking", False):
        payload["blocking"] = True
    if getattr(args, "faq", False):
        payload["faq"] = True
    if getattr(args, "ref", None):
        payload["refs"] = args.ref
    if getattr(args, "evidence", None):
        payload["evidence"] = [{"text": e} for e in args.evidence]
    return payload


def cmd_add(args):
    call = _api()
    m = _resolve_mission(call, args.mission)
    payload = _payload_from_flags(args)
    call("PUT", f"/api/mission/item/{m['mission_id']}/{args.pillar}/"
                f"{args.item_id}", payload)
    print(f"  ✓ {args.pillar}:{args.item_id}"
          f"  [{payload.get('kind', '?')}] {payload.get('title', '')}")


def cmd_update(args):
    call = _api()
    m = _resolve_mission(call, args.mission)
    items = call("GET", f"/api/mission/items/{m['mission_id']}"
                        f"?pillar={args.pillar}").get("items", [])
    current = next((i for i in items if i["item_id"] == args.item_id), None)
    if current is None:
        print(f"mission update: no item {args.item_id!r} on {args.pillar}",
              file=sys.stderr)
        sys.exit(1)
    payload = {k: v for k, v in current.items()
               if k not in ("key", "surface_id", "item_id",
                            "created_at", "updated_at")}
    payload.update(_payload_from_flags(args))
    # Repeatable flags APPEND on update — three real incidents of
    # silently wiped ref lists (coverage broke each time) proved that
    # replace semantics are a trap. Shrinking is explicit: --clear-refs
    # resets the list to exactly what this invocation provides.
    if getattr(args, "clear_refs", False):
        payload["refs"] = list(args.ref or [])
    elif getattr(args, "ref", None):
        existing = current.get("refs") or []
        payload["refs"] = existing + [r for r in args.ref
                                      if r not in existing]
    if getattr(args, "evidence", None):
        payload["evidence"] = (current.get("evidence") or []) + [
            {"text": e} for e in args.evidence]
    call("PUT", f"/api/mission/item/{m['mission_id']}/{args.pillar}/"
                f"{args.item_id}", payload)
    print(f"  ✓ {args.pillar}:{args.item_id} updated")


def _entry_verb(route: str, done: str):
    def run(args):
        call = _api()
        m = _resolve_mission(call, args.mission)
        text = _stdin(args.text)
        call("POST", f"/api/mission/item/{m['mission_id']}/{args.pillar}/"
                     f"{args.item_id}/{route}", {"text": text})
        print(f"  ✓ {args.pillar}:{args.item_id} {done}")
    return run


def cmd_state(args):
    call = _api()
    m = _resolve_mission(call, args.mission)
    body: dict = {"state": args.state}
    if args.turn:
        body["turn"] = args.turn
    call("POST", f"/api/mission/item/{m['mission_id']}/{args.pillar}/"
                 f"{args.item_id}/state", body)
    print(f"  ✓ {args.pillar}:{args.item_id} → {args.state}")


def cmd_chat(args):
    call = _api()
    m = _resolve_mission(call, args.mission)
    path = f"/api/mission/chat/{m['mission_id']}/{args.pillar}"
    if args.text:
        call("POST", path, {"text": _stdin(args.text)})
        print("  ✓ sent")
        return
    for e in call("GET", path).get("entries", []):
        print(f"[{e.get('at', '')}] {e.get('by', '?')}: {e.get('text', '')}")


def cmd_coverage(args):
    """Beads on the mission that no criterion covers (and the reverse)."""
    import subprocess
    call = _api()
    m = _resolve_mission(call, args.mission)
    mid = m["mission_id"]
    items = call("GET", f"/api/mission/items/{mid}").get("items", [])
    covered = set()
    checkpoints = 0
    for it in items:
        if it.get("kind") != "checkpoint":
            continue
        checkpoints += 1
        for ref in it.get("refs") or []:
            if str(ref).startswith("bead:"):
                covered.add(str(ref)[5:])
    tasks = call("GET", f"/api/mission/tasks/{mid}").get("tasks", {})
    all_tasks = [(pid, t) for pid, tl in tasks.items() for t in tl]
    uncovered = [(pid, t) for pid, t in all_tasks if t["id"] not in covered]
    print(f"{m['name']}: {len(all_tasks)} tasks, {checkpoints} criteria "
          f"covering {len(covered)}, {len(uncovered)} uncovered")
    last = None
    # dicts aren't orderable — sort by (pillar, bead id), never the dict
    for pid, t in sorted(uncovered, key=lambda x: (x[0], x[1]["id"])):
        if pid != last:
            print(f"  {pid}")
            last = pid
        mark = "epic " if t.get("epic") else ""
        print(f"    {t['id']:14s} {t['state']:9s} {mark}{t['title']}")
    stale = sorted(covered - {t["id"] for _, t in all_tasks})
    if stale:
        print("  criteria referencing beads not on the mission:")
        for bid in stale:
            print(f"    {bid}")
    del subprocess  # bd never consulted: the tasks route already derived


# ── registration ──────────────────────────────────────────────────


def register(sub) -> None:
    p = sub.add_parser(
        "mission",
        help="Drive a structured mission (the mission plugin's verbs)")
    p.set_defaults(func=lambda _a: p.print_help())
    ms = p.add_subparsers(dest="mission_subcmd")

    q = ms.add_parser("list", help="All missions")
    q.set_defaults(func=cmd_list)

    q = ms.add_parser("status", help="Delivery ladder, task ladder, and "
                                     "blockers per pillar")
    q.add_argument("mission")
    q.add_argument("pillar", nargs="?", help="Narrow to one pillar")
    q.add_argument("-v", "--verbose", action="store_true")
    q.set_defaults(func=cmd_status)

    q = ms.add_parser("items", help="List items")
    q.add_argument("mission")
    q.add_argument("--pillar")
    q.add_argument("--kind", choices=["scope", "status", "checkpoint",
                                      "decision", "question"])
    q.add_argument("--json", dest="json_output", action="store_true")
    q.set_defaults(func=cmd_items)

    def item_flags(qq):
        qq.add_argument("mission")
        qq.add_argument("pillar")
        qq.add_argument("item_id")
        qq.add_argument("--kind", choices=["scope", "status", "checkpoint",
                                           "decision", "question"])
        qq.add_argument("--title")
        qq.add_argument("--body", help="Markdown prose; '-' reads stdin")
        qq.add_argument("--state", choices=["confirmed", "in_progress",
                                            "pending", "open", "answered"])
        qq.add_argument("--section")
        qq.add_argument("--order", type=float)
        qq.add_argument("--ref", action="append",
                        help="Repeatable: bead:<id> commit:<sha> graph:<id>. "
                             "On update, APPENDS to the existing list")
        qq.add_argument("--clear-refs", dest="clear_refs",
                        action="store_true",
                        help="Update only: reset refs to exactly the "
                             "--ref values given (empty if none)")
        qq.add_argument("--evidence", action="append",
                        help="Repeatable evidence text (provenance-bearing "
                             "entries via --from)")
        qq.add_argument("--ask")
        qq.add_argument("--blocking", action="store_true",
                        help="Open question preventing forward progress")
        qq.add_argument("--asked-by", dest="asked_by")
        qq.add_argument("--asked-at", dest="asked_at")
        qq.add_argument("--fork")
        qq.add_argument("--chosen")
        qq.add_argument("--if-wrong", dest="if_wrong")
        qq.add_argument("--faq", action="store_true",
                        help="Pin a decision as favorite/FAQ")
        qq.add_argument("--owner")
        qq.add_argument("--happened-at", dest="happened_at")
        qq.add_argument("--from", dest="from_file",
                        help="JSON file with the full payload; flags override")

    q = ms.add_parser("add", help="Create (or fully rewrite) one item")
    item_flags(q)
    q.set_defaults(func=cmd_add)

    q = ms.add_parser("update", help="Patch fields on an existing item")
    item_flags(q)
    q.set_defaults(func=cmd_update)

    q = ms.add_parser("state", help="Transition a checkpoint (history "
                                    "appended, confirmation stamped)")
    q.add_argument("mission")
    q.add_argument("pillar")
    q.add_argument("item_id")
    q.add_argument("state", choices=["confirmed", "in_progress", "pending"])
    q.add_argument("--turn", type=int,
                   help="Turn in the confirming session")
    q.set_defaults(func=cmd_state)

    for verb, route, done, hlp in (
        ("work", "work", "work logged",
         "Append an attributed work entry to a checkpoint"),
        ("reply", "reply", "reply added",
         "Reply on an open question (a reply is NOT an answer)"),
        ("progress", "progress", "progress posted",
         "Transient status while working out an answer"),
        ("answer", "answer", "answered",
         "File the one cohesive answer; closes the question"),
    ):
        q = ms.add_parser(verb, help=hlp)
        q.add_argument("mission")
        q.add_argument("pillar")
        q.add_argument("item_id")
        q.add_argument("text", help="'-' reads stdin")
        q.set_defaults(func=_entry_verb(route, done))

    q = ms.add_parser("chat", help="Send to (or read) a pillar's chat log")
    q.add_argument("mission")
    q.add_argument("pillar")
    q.add_argument("text", nargs="?", help="Message; omit to read the log; "
                                           "'-' reads stdin")
    q.set_defaults(func=cmd_chat)

    q = ms.add_parser("coverage", help="Tasks no acceptance criterion covers")
    q.add_argument("mission")
    q.set_defaults(func=cmd_coverage)
