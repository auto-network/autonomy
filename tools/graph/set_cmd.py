"""``graph set`` subcommand group — Settings primitive CLI.

Spec: graph://0d3f750f-f9c. Thin layer over ``settings_ops``, dispatched
via :func:`tools.graph.client.get_client` so container CLIs route through
the dashboard API instead of opening local sqlite files.

The argparse setup lives in cli.py; per-subcommand handlers live here.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from .client import get_client
from .schemas.registry import SchemaValidationError


_VALID_PROMOTION_STATES = ("curated", "published", "canonical")


# ── helpers ──────────────────────────────────────────────────


def _read_payload_file(path: str) -> dict:
    """Load a payload from JSON or YAML. Falls back to JSON if PyYAML missing.

    ``path == "-"`` reads from stdin (JSON only — YAML over stdin would
    require sniffing or an explicit format flag).
    """
    if path == "-":
        text = sys.stdin.read()
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            print(f"Error: invalid JSON on stdin: {e}", file=sys.stderr)
            sys.exit(1)
        if not isinstance(data, dict):
            print("Error: stdin must contain a JSON object", file=sys.stderr)
            sys.exit(1)
        return data
    text = Path(path).read_text()
    p = path.lower()
    if p.endswith((".yaml", ".yml")):
        try:
            import yaml  # type: ignore[import-not-found]
        except ImportError:
            print("Error: PyYAML not installed; pass a .json file or install pyyaml.",
                  file=sys.stderr)
            sys.exit(1)
        data = yaml.safe_load(text)
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            print(f"Error: invalid JSON in {path}: {e}", file=sys.stderr)
            sys.exit(1)
    if not isinstance(data, dict):
        print(f"Error: {path} must contain a JSON/YAML object", file=sys.stderr)
        sys.exit(1)
    return data


def _resolve_payload_input(args) -> dict:
    """Pick the payload source for ``set add`` / ``set override``.

    Order of precedence (mutually exclusive — checked at parse time):

    * ``--inline JSON`` — small payloads on the command line.
    * ``--from PATH`` (``-`` reads stdin) — file path.
    * neither, and stdin is not a TTY — implicit stdin read.
    * neither, stdin is a TTY — error (no payload supplied).
    """
    inline = getattr(args, "inline", None)
    from_path = getattr(args, "from_file", None)
    if inline is not None and from_path is not None:
        print("Error: --inline and --from are mutually exclusive",
              file=sys.stderr)
        sys.exit(1)
    if inline is not None:
        try:
            data = json.loads(inline)
        except json.JSONDecodeError as e:
            print(f"Error: invalid JSON for --inline: {e}", file=sys.stderr)
            sys.exit(1)
        if not isinstance(data, dict):
            print("Error: --inline must be a JSON object", file=sys.stderr)
            sys.exit(1)
        return data
    if from_path is not None:
        return _read_payload_file(from_path)
    if not sys.stdin.isatty():
        return _read_payload_file("-")
    print("Error: no payload supplied — pass --inline JSON, --from FILE, "
          "--from -, or pipe JSON on stdin", file=sys.stderr)
    sys.exit(1)


def _parse_set_at_rev(spec: str) -> tuple[str, int]:
    """Parse 'autonomy.workspace#1' into ('autonomy.workspace', 1)."""
    if "#" not in spec:
        print(f"Error: expected <set_id>#<rev>, got {spec!r}", file=sys.stderr)
        sys.exit(1)
    set_id, rev = spec.rsplit("#", 1)
    try:
        return set_id, int(rev)
    except ValueError:
        print(f"Error: revision must be an integer in {spec!r}", file=sys.stderr)
        sys.exit(1)


def _print_table(rows: list[dict], cols: list[tuple[str, str, int]]) -> None:
    """Print a table. ``cols`` = (key, header, width)."""
    header = "  ".join(f"{h:<{w}}" for _, h, w in cols)
    sep = "  ".join("─" * w for _, _, w in cols)
    print(header)
    print(sep)
    for r in rows:
        line = "  ".join(
            f"{str(r.get(k, '') or '')[:w]:<{w}}" for k, _, w in cols
        )
        print(line)


# ── address resolution ──────────────────────────────────────


def _looks_like_set_id(value: str) -> bool:
    """Heuristic: dotted set_id (``autonomy.workspace``) vs UUID/prefix.

    UUIDs/prefixes are hex with dashes only; set_ids contain dots. Used
    only to decide between addressing modes when two positionals were
    given — we still attempt resolution and surface a clear error if the
    guess was wrong.
    """
    return "." in value


def _resolve_address(args, *, accept_key: bool = True) -> str:
    """Resolve the ``id [key]`` positional pair to a single Setting id.

    * 1 positional → treat as full id or prefix; call
      ``client.resolve_setting_strict``.
    * 2 positionals (and ``accept_key`` is True) → treat as
      ``(set_id, key)``; call ``client.read_set`` and pick the member
      with matching key.

    On miss / ambiguity the function prints a candidate list (for
    ambiguity) or a "no match" error and ``sys.exit(1)``s. On success
    it returns the canonical id string.
    """
    org = _org(args)
    parts: list[str] = list(getattr(args, "id_parts", []) or [])
    if not parts:
        print("Error: missing positional argument: id (or set_id key)",
              file=sys.stderr)
        sys.exit(1)
    if len(parts) > 2:
        print(f"Error: too many positional arguments: {parts!r}",
              file=sys.stderr)
        sys.exit(1)

    if len(parts) == 2:
        if not accept_key:
            print(f"Error: this command takes one positional id, got {parts!r}",
                  file=sys.stderr)
            sys.exit(1)
        set_id, key = parts
        return _resolve_set_key(set_id, key, org=org)

    # Single positional — id or prefix.
    return _resolve_id_or_prefix(parts[0], org=org)


def _resolve_id_or_prefix(value: str, *, org: str | None) -> str:
    """Resolve an id or id-prefix to a single Setting id.

    Prints diagnostics + ``sys.exit(1)`` on miss or ambiguity.
    """
    client = get_client()
    hit = client.resolve_setting_strict(value, org=org)
    if hit is None:
        scope = f" in org {org!r}" if org else ""
        print(
            f"Error: no Setting with id starting with {value!r}{scope}",
            file=sys.stderr,
        )
        sys.exit(1)
    if isinstance(hit, list):
        print(
            f"Error: ambiguous prefix {value!r} matches {len(hit)} Settings:",
            file=sys.stderr,
        )
        for r in hit:
            sid = r.get("id", "")
            sset = r.get("set_id", "")
            skey = r.get("key", "")
            print(f"  {sid}  {sset}  key={skey}", file=sys.stderr)
        sys.exit(1)
    return hit["id"]


def _resolve_set_key(set_id: str, key: str, *, org: str | None) -> str:
    """Resolve ``(set_id, key)`` to the winning base Setting id.

    Uses ``client.read_set`` so the answer matches what consumers see.
    Errors with ``sys.exit(1)`` if no member matches.
    """
    client = get_client()
    members = client.read_set(set_id, org=org)
    for m in members.members:
        if m.key == key:
            return m.id
    scope = f" in org {org!r}" if org else ""
    print(
        f"Error: no Setting with set_id={set_id!r} key={key!r}{scope}",
        file=sys.stderr,
    )
    sys.exit(1)


# ── list / members / show / read ────────────────────────────


def _org(args) -> str | None:
    return getattr(args, "org", None)


def cmd_set_list(args) -> None:
    set_ids = get_client().list_set_ids(org=_org(args))
    if not set_ids:
        print("(no Settings yet)")
        return
    for s in set_ids:
        print(s)


def _resolve_read_flags(args) -> tuple[int | None, int | None, int | None, bool]:
    """Returns (target_revision, min_revision, stored_revision, no_upconvert)."""
    return (
        getattr(args, "as_rev", None),
        getattr(args, "min_rev", None),
        getattr(args, "stored_rev", None),
        bool(getattr(args, "no_upconvert", False)),
    )


def cmd_set_members(args) -> None:
    target, minrev, stored, no_upconvert = _resolve_read_flags(args)
    if no_upconvert:
        target = None  # explicit "show stored shape"
    members = get_client().read_set(
        args.set_id, target_revision=target, min_revision=minrev,
        org=_org(args),
    )
    rows = []
    for m in members.members:
        if stored is not None and m.stored_revision != stored:
            continue
        target_disp = (
            str(m.target_revision) if m.target_revision is not None else "-"
        )
        rows.append({
            "id": m.id[:11],
            "key": m.key,
            "stored_rev": str(m.stored_revision),
            "target_rev": target_disp,
            "state": m.state,
        })
    if not rows:
        print(f"(no Settings in {args.set_id})")
    else:
        _print_table(rows, [
            ("id", "ID", 12),
            ("key", "KEY", 28),
            ("stored_rev", "STORED", 6),
            ("target_rev", "TARGET", 6),
            ("state", "STATE", 10),
        ])
    if any(members.dropped.values()):
        print()
        print(f"Dropped: {members.dropped.to_dict()}")


def cmd_set_show(args) -> None:
    target, _, _, no_upconvert = _resolve_read_flags(args)
    if no_upconvert:
        target = None
    sid = _resolve_address(args)
    got = get_client().get_setting(
        sid, target_revision=target, org=_org(args),
    )
    if got is None:
        suffix = " (or dropped by --as-rev)" if target is not None else ""
        print(f"Error: Setting not found: {sid}{suffix}",
              file=sys.stderr)
        sys.exit(1)
    out = {
        "id": got.id,
        "set_id": got.set_id,
        "stored_revision": got.stored_revision,
        "target_revision": got.target_revision,
        "key": got.key,
        "state": got.state,
        "supersedes": got.supersedes,
        "excludes": got.excludes,
        "deprecated": got.deprecated,
        "successor_id": got.successor_id,
        "created_at": got.created_at,
        "updated_at": got.updated_at,
        "payload": got.payload,
    }
    print(json.dumps(out, indent=2))


def cmd_set_read(args) -> None:
    """``graph set read <id-or-prefix> | <set_id> <key>`` — resolved payload.

    Mirrors ``graph read <src_id>`` semantics: returns the effective
    content the consumer sees at runtime. With ``--chain``, walks the
    supersedes chain and shows per-layer contributions.
    """
    parts: list[str] = list(getattr(args, "id_parts", []) or [])
    org = _org(args)
    if not parts:
        print("Error: missing positional argument: id (or set_id key)",
              file=sys.stderr)
        sys.exit(1)
    if len(parts) > 2:
        print(f"Error: too many positional arguments: {parts!r}",
              file=sys.stderr)
        sys.exit(1)

    chain_mode = bool(getattr(args, "chain", False))

    if len(parts) == 2:
        set_id, key = parts
    else:
        # Single positional — resolve to row, then derive (set_id, key).
        client = get_client()
        hit = client.resolve_setting_strict(parts[0], org=org)
        if hit is None:
            scope = f" in org {org!r}" if org else ""
            print(
                f"Error: no Setting with id starting with {parts[0]!r}{scope}",
                file=sys.stderr,
            )
            sys.exit(1)
        if isinstance(hit, list):
            print(
                f"Error: ambiguous prefix {parts[0]!r} matches "
                f"{len(hit)} Settings:",
                file=sys.stderr,
            )
            for r in hit:
                print(f"  {r.get('id','')}  {r.get('set_id','')}  "
                      f"key={r.get('key','')}", file=sys.stderr)
            sys.exit(1)
        set_id = hit["set_id"]
        key = hit["key"]

    if chain_mode:
        chain = get_client().chain_setting(set_id, key, org=org)
        if chain is None:
            scope = f" in org {org!r}" if org else ""
            print(
                f"Error: no member matches ({set_id!r}, {key!r}){scope}",
                file=sys.stderr,
            )
            sys.exit(1)
        print(json.dumps(chain, indent=2))
        return

    members = get_client().read_set(set_id, org=org)
    for m in members.members:
        if m.key == key:
            print(json.dumps(m.payload, indent=2, default=str))
            return
    scope = f" in org {org!r}" if org else ""
    print(
        f"Error: no member matches ({set_id!r}, {key!r}){scope}",
        file=sys.stderr,
    )
    sys.exit(1)


# ── add / override / exclude ────────────────────────────────


def cmd_set_add(args) -> None:
    set_id, rev = _parse_set_at_rev(args.set_at_rev)
    payload = _resolve_payload_input(args)
    try:
        sid = get_client().add_setting(
            set_id, rev, args.key, payload, state=args.state,
            org=_org(args),
        )
    except SchemaValidationError as e:
        print(f"Error: schema validation failed: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"  ✓ Setting: {sid[:11]}  {set_id}#{rev}  key={args.key}  [{args.state}]")


def cmd_set_override(args) -> None:
    payload = _resolve_payload_input(args)
    target_id = _resolve_target_address(args)
    try:
        sid = get_client().override_setting(
            target_id, payload, state=args.state,
            org=_org(args),
        )
    except LookupError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except SchemaValidationError as e:
        print(f"Error: merged payload fails validation: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"  ✓ Override: {sid[:11]}  supersedes={target_id[:11]}  [{args.state}]")


def cmd_set_exclude(args) -> None:
    target_id = _resolve_target_address(args)
    try:
        sid = get_client().exclude_setting(
            target_id, state=args.state, org=_org(args),
        )
    except LookupError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"  ✓ Exclude: {sid[:11]}  excludes={target_id[:11]}  [{args.state}]")


def _resolve_target_address(args) -> str:
    """Resolve the target positional (id-or-prefix or set_id+key) for
    override/exclude. The two argparse positionals come in as ``target``
    and (optionally) ``target_key``."""
    target = getattr(args, "target", None)
    target_key = getattr(args, "target_key", None)
    org = _org(args)
    if not target:
        print("Error: missing target id (or set_id key)", file=sys.stderr)
        sys.exit(1)
    if target_key is not None:
        return _resolve_set_key(target, target_key, org=org)
    return _resolve_id_or_prefix(target, org=org)


# ── promote / deprecate / remove ────────────────────────────


def cmd_set_promote(args) -> None:
    if args.to not in _VALID_PROMOTION_STATES:
        print(
            f"Error: --to must be one of {_VALID_PROMOTION_STATES}, got {args.to!r}",
            file=sys.stderr,
        )
        sys.exit(1)
    sid = _resolve_address(args)
    try:
        get_client().promote_setting(sid, args.to, org=_org(args))
    except LookupError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"  ✓ Promoted: {sid[:11]} → {args.to}")


def cmd_set_deprecate(args) -> None:
    sid = _resolve_address(args)
    try:
        get_client().deprecate_setting(
            sid, successor_id=args.successor,
            org=_org(args),
        )
    except LookupError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    suc = f" successor={args.successor[:11]}" if args.successor else ""
    print(f"  ✓ Deprecated: {sid[:11]}{suc}")


def cmd_set_remove(args) -> None:
    sid = _resolve_address(args)
    try:
        get_client().remove_setting(sid, org=_org(args))
    except LookupError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"  ✓ Removed: {sid[:11]}")


# ── migrate ─────────────────────────────────────────────────


def cmd_set_migrate(args) -> None:
    try:
        report = get_client().migrate_setting_revisions(
            args.set_id, args.to_rev, dry_run=args.dry_run,
            org=_org(args),
        )
    except Exception as e:  # noqa: BLE001 — surface to operator
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    mode = "DRY RUN" if args.dry_run else "WRITE"
    print(f"[{mode}] migrate {args.set_id} → rev {args.to_rev}")
    print(f"  rewrote:            {report.rewrote}")
    print(f"  already_at_target:  {report.already_at_target}")
    print(f"  above_target:       {report.above_target}")
    print(f"  no_upconvert_path:  {report.no_upconvert_path}")
    if report.affected_ids:
        print("  affected:")
        for sid in report.affected_ids[:20]:
            print(f"    {sid}")
        if len(report.affected_ids) > 20:
            print(f"    ... ({len(report.affected_ids) - 20} more)")


# ── argparse setup ──────────────────────────────────────────


def add_read_flags(parser) -> None:
    parser.add_argument("--as-rev", type=int, dest="as_rev",
                        help="Target revision: shape every returned row at rev N")
    parser.add_argument("--min-rev", type=int, dest="min_rev",
                        help="Floor: drop rows with stored_revision < N")
    parser.add_argument("--stored-rev", type=int, dest="stored_rev",
                        help="Filter: only rows whose stored_revision is exactly N")
    parser.add_argument("--no-upconvert", action="store_true",
                        dest="no_upconvert",
                        help="Show stored payloads (the default; explicit form)")


def _add_org_arg(parser) -> None:
    parser.add_argument(
        "--org", dest="org", default=None,
        help="Route to the per-org DB at data/orgs/<slug>.db "
             "(default: autonomy / GRAPH_DB env)",
    )


def _add_address_arg(parser, *, help_text: str) -> None:
    """Wire the dual positional ``id [key]`` form onto a subparser."""
    parser.add_argument(
        "id_parts", nargs="+", metavar="ID",
        help=help_text,
    )


def attach_set_subparser(sub) -> None:
    """Wire up ``graph set ...`` subcommands onto an existing subparsers obj."""
    p_set = sub.add_parser(
        "set",
        help="Settings primitive: layered configuration (graph://0d3f750f-f9c)",
    )
    set_sub = p_set.add_subparsers(dest="set_subcmd", required=True)

    # list
    p_list = set_sub.add_parser("list", help="List known set_ids visible to caller")
    _add_org_arg(p_list)
    p_list.set_defaults(func=cmd_set_list)

    # members
    p_members = set_sub.add_parser(
        "members", help="List resolved members of a SET (default: stored revisions)",
    )
    p_members.add_argument("set_id")
    add_read_flags(p_members)
    _add_org_arg(p_members)
    p_members.set_defaults(func=cmd_set_members)

    # show
    p_show = set_sub.add_parser("show", help="Show a single Setting in detail")
    _add_address_arg(
        p_show,
        help_text="Setting id (full or unique prefix), or `set_id key`",
    )
    add_read_flags(p_show)
    _add_org_arg(p_show)
    p_show.set_defaults(func=cmd_set_show)

    # read — resolved payload by (set_id, key) or id-prefix
    p_read = set_sub.add_parser(
        "read",
        help="Resolved effective payload (post-merge) for a Setting member",
    )
    _add_address_arg(
        p_read,
        help_text="`set_id key`, or a Setting id (full or unique prefix)",
    )
    p_read.add_argument(
        "--chain", action="store_true", dest="chain",
        help="Show per-layer contributions through the supersedes chain",
    )
    _add_org_arg(p_read)
    p_read.set_defaults(func=cmd_set_read)

    # add
    p_add = set_sub.add_parser("add", help="Create a base Setting")
    p_add.add_argument("set_at_rev",
                       help="set_id#schema_revision, e.g. autonomy.workspace#1")
    p_add.add_argument("--key", required=True, help="Identity within (set_id, this DB)")
    p_add.add_argument("--from", dest="from_file",
                       help="Path to JSON or YAML payload file (use '-' for stdin)")
    p_add.add_argument("--inline", dest="inline",
                       help="Inline JSON object (mutually exclusive with --from)")
    p_add.add_argument("--state", default="raw",
                       choices=("raw", "curated", "published", "canonical"))
    _add_org_arg(p_add)
    p_add.set_defaults(func=cmd_set_add)

    # override
    p_over = set_sub.add_parser("override", help="Create an override Setting")
    p_over.add_argument(
        "target", metavar="ID",
        help="Target Setting id (full or unique prefix)",
    )
    p_over.add_argument(
        "target_key", metavar="KEY", nargs="?",
        help="Optional: when given, treat the previous arg as set_id and "
             "look up by (set_id, key)",
    )
    p_over.add_argument("--from", dest="from_file",
                        help="Path to partial JSON/YAML payload (use '-' for stdin)")
    p_over.add_argument("--inline", dest="inline",
                        help="Inline JSON object (mutually exclusive with --from)")
    p_over.add_argument("--state", default="raw",
                        choices=("raw", "curated", "published", "canonical"))
    _add_org_arg(p_over)
    p_over.set_defaults(func=cmd_set_override)

    # exclude
    p_excl = set_sub.add_parser("exclude", help="Create an exclude Setting")
    p_excl.add_argument(
        "target", metavar="ID",
        help="Target Setting id (full or unique prefix)",
    )
    p_excl.add_argument(
        "target_key", metavar="KEY", nargs="?",
        help="Optional: when given, treat the previous arg as set_id and "
             "look up by (set_id, key)",
    )
    p_excl.add_argument("--state", default="raw",
                        choices=("raw", "curated", "published", "canonical"))
    _add_org_arg(p_excl)
    p_excl.set_defaults(func=cmd_set_exclude)

    # promote
    p_prom = set_sub.add_parser("promote", help="Transition publication_state")
    _add_address_arg(
        p_prom,
        help_text="Setting id (full or unique prefix), or `set_id key`",
    )
    p_prom.add_argument("--to", required=True,
                        choices=_VALID_PROMOTION_STATES)
    _add_org_arg(p_prom)
    p_prom.set_defaults(func=cmd_set_promote)

    # deprecate
    p_dep = set_sub.add_parser("deprecate", help="Mark a Setting deprecated")
    _add_address_arg(
        p_dep,
        help_text="Setting id (full or unique prefix), or `set_id key`",
    )
    p_dep.add_argument("--successor", help="Optional successor Setting id")
    _add_org_arg(p_dep)
    p_dep.set_defaults(func=cmd_set_deprecate)

    # remove
    p_rem = set_sub.add_parser("remove", help="Hard-delete a raw Setting")
    _add_address_arg(
        p_rem,
        help_text="Setting id (full or unique prefix), or `set_id key`",
    )
    _add_org_arg(p_rem)
    p_rem.set_defaults(func=cmd_set_remove)

    # migrate
    p_mig = set_sub.add_parser(
        "migrate", help="Rewrite stored rows up to a target revision",
    )
    p_mig.add_argument("set_id")
    p_mig.add_argument("--to-rev", type=int, required=True, dest="to_rev")
    p_mig.add_argument("--dry-run", action="store_true", dest="dry_run")
    _add_org_arg(p_mig)
    p_mig.set_defaults(func=cmd_set_migrate)
