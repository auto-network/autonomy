"""``graph workspace doctor`` — every unmet workspace requirement, in one report.

A thin renderer over ``GET /api/orgs/{slug}/workspaces/health``: the dashboard
runs the readiness walk (agents/workspace_readiness.py) and groups findings
into things to fix; this prints them so a reader learns, for each, what it is,
where the requirement comes from, and how to obtain it. No checks live here.

Exit status answers "will these workspaces launch": 1 when anything blocks or
could not be answered where it was asked, 0 otherwise.
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
import urllib.parse

#: Short state labels per finding kind. The org settings screen carries the
#: same words (static/js/org-settings.js STATE); an unknown kind prints as is.
KIND_LABELS = {
    "missing_reference": "Never provisioned here",
    "missing_env": "Not set",
    "missing_path": "Not on this machine",
    "unpopulated_path": "Present, but empty",
    "invalid_mount": "Mount declaration is unusable",
    "missing_capability_install": "No organization installation",
    "missing_capability_contract_version": "Contract version is unavailable",
    "capability_contract_version_mismatch": "Contract version does not match",
    "capability_install_contract_version_mismatch": "Install version does not match",
    "missing_capability_implementation_version": "Implementation version is unavailable",
    "capability_implementation_contract_mismatch": "Implementation contract does not match",
    "missing_vault_credential": "Not in the vault",
    "unreadable_vault": "Vault could not be read",
    "machine_mount_elsewhere": "Lives on another machine",
    "unanswerable_here": "Cannot be answered from here",
    "unreadable": "Could not be read",
    "unreadable_reference": "Exists, but not readable from here",
    "unknown_target": "No schema for this",
}

_WIDTH = 76
_LABEL = 13


def _section(thing: dict) -> str:
    if thing.get("kind") == "unanswerable_here":
        return "unanswerable"
    return "advisory" if thing.get("severity") == "advisory" else "blocking"


def _title(thing: dict) -> str:
    if thing.get("name"):
        return str(thing["name"])
    if thing.get("description"):
        return str(thing["description"]).split("—")[0].strip()
    return str(thing.get("subject") or thing.get("at") or "")


def _how(thing: dict) -> str:
    """The declaration's own help first; else what the remediation does."""
    if thing.get("help"):
        return str(thing["help"])
    rid = thing.get("remediation_id")
    if not rid:
        return ""
    from tools.graph.remediation import get_remediation

    spec = get_remediation(rid)
    if spec is None:
        return rid
    return f"{spec.label}: {spec.description}"


def _origin(thing: dict) -> str:
    where = thing.get("set_id") or ""
    if thing.get("key"):
        where += f" key {thing['key']!r}"
    if thing.get("field"):
        where = f"{thing['field']} on {where}"
    return where


def _pair(out: list[str], label: str, value: str) -> None:
    if not value:
        return
    lines = textwrap.wrap(str(value), width=_WIDTH - 6 - _LABEL) or [""]
    out.append(f"      {label:<{_LABEL}}{lines[0]}")
    out.extend(f"      {'':<{_LABEL}}{line}" for line in lines[1:])


def render(report: dict) -> str:
    """The report as text. Deterministic: same input, same bytes."""
    things = sorted(
        report.get("things") or [],
        key=lambda t: (
            ("blocking", "unanswerable", "advisory").index(_section(t)),
            _title(t).lower(), str(t.get("subject") or ""),
        ),
    )
    spaces = report.get("workspaces") or []
    ready = sum(1 for w in spaces if w.get("ready"))
    out = [
        f"Workspace doctor · org {report.get('org')} · "
        f"checked on {report.get('asked_in') or 'this process'}",
        f"{len(spaces)} workspace{'' if len(spaces) == 1 else 's'}: "
        f"{ready} ready, {len(spaces) - ready} not ready",
    ]
    heads = {
        "blocking": "Blocks launch — fix each of these",
        "unanswerable": "Not answered — ask where the answer lives",
        "advisory": "Advisory — workspaces still launch without these",
    }
    for section in ("blocking", "unanswerable", "advisory"):
        group = [t for t in things if _section(t) == section]
        if not group:
            continue
        out += ["", f"{heads[section]} ({len(group)})"]
        for t in group:
            mark = "-" if section == "advisory" else "✗"
            out.append(f"  {mark} {_title(t)} — "
                       f"{KIND_LABELS.get(t.get('kind'), t.get('kind'))}")
            _pair(out, "What it is", t.get("description") or "")
            reasons = t.get("reasons") or [
                {"kind": t.get("kind"), "what": t.get("what")}]
            for i, r in enumerate(reasons):
                _pair(out, "Why" if i == 0 else "", r.get("what") or "")
            _pair(out, "Needed by", ", ".join(t.get("needed_by") or []))
            _pair(out, "Comes from", _origin(t))
            _pair(out, "How to get", _how(t))
            _pair(out, "Checked in", t.get("looked_in") or "")
    if not things:
        out += ["", "Nothing missing: everything these workspaces declare is "
                    "present here."]
    out += ["", "Workspaces"]
    for w in sorted(spaces, key=lambda w: str(w.get("id"))):
        if w.get("ready"):
            state = "ready"
        else:
            n = w.get("unresolved", 0)
            state = f"not ready ({n} thing{'' if n == 1 else 's'})"
        label = w.get("name") or w.get("id")
        suffix = f"  {label}" if label != w.get("id") else ""
        out.append(f"  {'✓' if w.get('ready') else '✗'} {w.get('id')}{suffix} — {state}")
    return "\n".join(out)


def blocks(report: dict) -> bool:
    return any(not w.get("ready") for w in report.get("workspaces") or [])


def _caller_org(client) -> str | None:
    """A session's org, from its own record (stamped on its token)."""
    name = os.environ.get("AUTONOMY_SESSION")
    if not name:
        return None
    try:
        record = client.get_session_record(name) or {}
    except Exception:
        return None
    org = record.get("org")
    return (org or {}).get("slug") if isinstance(org, dict) else None


def cmd_workspace_doctor(args) -> None:
    from .client import get_client

    client = get_client()
    org = getattr(args, "org", None) or _caller_org(client)
    if not org:
        print("Error: no organization. Pass --org SLUG (outside a session "
              "there is no caller organization to default to).", file=sys.stderr)
        sys.exit(2)
    params = {"workspace": args.workspace} if args.workspace else None
    try:
        report = client._get(
            f"/api/orgs/{urllib.parse.quote(org, safe='')}/workspaces/health",
            params=params, org=org,
        )
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(2)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render(report))
    sys.exit(1 if blocks(report) else 0)


def add_parser(sub) -> None:
    p = sub.add_parser("workspace", help="Workspace readiness on this machine")
    ws_sub = p.add_subparsers(dest="workspace_cmd", required=True)
    d = ws_sub.add_parser(
        "doctor",
        help="Report every unmet workspace requirement: what it is, where it "
             "comes from, how to obtain it",
    )
    d.add_argument("--org", help="Organization slug (default: this session's)")
    d.add_argument("--workspace", help="Check one workspace id only")
    d.add_argument("--json", action="store_true", help="Print the raw report")
    d.set_defaults(func=cmd_workspace_doctor)
