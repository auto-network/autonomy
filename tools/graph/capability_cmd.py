"""``graph capability`` subcommand group.

Today: ``graph capability skilltext`` — generate ``SKILL.md`` /
``primer.md`` projections from a capability contract Setting. Reads
the contract from the registry (``read_setting``) and routes the
content through :mod:`tools.graph.capability_skilltext`.

The subparser is wired into ``cli.py`` via :func:`attach_capability_subparser`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .client import get_client
from . import capability_skilltext


# ── helpers ──────────────────────────────────────────────────


def _load_provider_manifest(path: str | None) -> dict | None:
    if not path:
        return None
    p = Path(path)
    text = p.read_text()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        print(f"Error: invalid JSON in {path}: {exc}", file=sys.stderr)
        sys.exit(1)


def _load_contract(name: str, version: int, *, org: str | None) -> dict:
    """Resolve a contract Setting by ``(name, version)`` and return its
    payload.

    Routes through :func:`get_client` so this command works the same in
    container (HTTP to the dashboard) and host (direct ops) contexts.
    Raises with a clear message when the contract isn't registered yet
    so an operator who's run ``graph capability skilltext`` against a
    name that hasn't been seeded gets actionable feedback rather than a
    quiet empty render.
    """
    client = get_client()
    members = client.read_set("autonomy.capability.contract", org=org)
    member_list = getattr(members, "members", members)
    for resolved in member_list:
        payload = (
            resolved.get("payload") if isinstance(resolved, dict)
            else getattr(resolved, "payload", None)
        ) or {}
        if payload.get("name") == name and int(payload.get("version", 0)) == version:
            return payload
    raise SystemExit(
        f"capability contract not found: {name}@{version}. "
        f"Run 'graph set members autonomy.capability.contract' to list "
        f"registered contracts."
    )


# ── commands ─────────────────────────────────────────────────


def cmd_skilltext(args: Any) -> None:
    from . import ops
    contract = _load_contract(
        args.contract, int(args.version),
        org=getattr(args, "org", None) or ops.CALLER_ORG,
    )
    provider = _load_provider_manifest(getattr(args, "provider_manifest", None))
    if args.kind == "skill":
        out = capability_skilltext.render_skill_md(contract, provider=provider)
    elif args.kind == "primer":
        out = capability_skilltext.render_primer_md(contract, provider=provider)
    else:
        print(f"Error: unknown --kind {args.kind!r}", file=sys.stderr)
        sys.exit(2)
    if args.to == "-" or not args.to:
        sys.stdout.write(out)
        return
    target = Path(args.to)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(out)
    print(f"Wrote {len(out)} bytes to {target}", file=sys.stderr)


def cmd_host_install(args: Any) -> None:
    """Run the host-install runner for one impl or every impl.

    Walks ``agents/capabilities/*/manifest.json`` declaring ``host_install``
    (or the single named ``impl``), fingerprints intent files, installs
    under a per-impl lock when stale, and upserts
    ``dashboard.capability.host_install_state#1`` rows. Exit status is
    non-zero when any impl ended in ``failed`` / ``error`` so callers (and
    CI) can gate on it; ``ready`` / ``skipped`` / ``unknown`` / ``locked``
    all exit 0.
    """
    from . import capability_host_install as runner
    from . import ops

    impl = getattr(args, "impl", None)
    org = getattr(args, "org", None) or ops.CALLER_ORG
    results = runner.run(
        impl,
        client=get_client(),
        org=org,
        log_fn=lambda msg: print(msg, file=sys.stderr),
    )

    if impl is not None and not results:
        print(
            f"Error: no capability impl matching {impl!r} declares "
            f"host_install. Run 'graph capability host-install' with no "
            f"argument to list installable impls.",
            file=sys.stderr,
        )
        sys.exit(1)

    if not results:
        print("No capability impls declare host_install.", file=sys.stderr)
        return

    failed = [r for r in results if r.get("outcome") in ("failed", "error")]
    for r in results:
        print(f"{r['outcome']:>8}  {r['name']}")
    if failed:
        sys.exit(1)


# ── parser wiring ────────────────────────────────────────────


def attach_capability_subparser(sub) -> None:
    p_cap = sub.add_parser(
        "capability",
        help="Capability-layer codegen + tooling",
    )
    cap_sub = p_cap.add_subparsers(dest="capability_cmd")
    cap_sub.required = True

    p_skill = cap_sub.add_parser(
        "skilltext",
        help="Generate SKILL.md / primer.md from a capability contract",
    )
    p_skill.add_argument(
        "--contract", required=True,
        help="Contract name (e.g. source_control)",
    )
    p_skill.add_argument(
        "--version", default=1, type=int,
        help="Contract version (default: 1)",
    )
    p_skill.add_argument(
        "--kind", default="skill", choices=("skill", "primer"),
        help="Which projection to render (default: skill)",
    )
    p_skill.add_argument(
        "--provider-manifest", default=None,
        help="Path to a capability-impl manifest.json — adds provider"
             " context to the rendered frontmatter and intro.",
    )
    p_skill.add_argument(
        "--to", default="-",
        help="Output path. '-' (default) writes to stdout.",
    )
    p_skill.add_argument(
        "--org", default=None,
        help="Read the contract from this org's DB. Defaults to the"
             " caller-org (env GRAPH_ORG or per-process default).",
    )
    p_skill.set_defaults(func=cmd_skilltext)

    p_hi = cap_sub.add_parser(
        "host-install",
        help="Run the host-install runner for one impl (or every impl "
             "declaring host_install)",
    )
    p_hi.add_argument(
        "impl", nargs="?", default=None,
        help="Implementation to install (e.g. autonomy/video or video). "
             "Omit to walk every impl declaring host_install.",
    )
    p_hi.add_argument(
        "--org", default=None,
        help="Write state rows into this org's DB. Defaults to the "
             "caller-org (env GRAPH_ORG or per-process default).",
    )
    p_hi.set_defaults(func=cmd_host_install)
