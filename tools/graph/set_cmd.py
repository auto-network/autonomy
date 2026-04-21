"""``graph set`` subcommand group — Settings primitive CLI.

Spec: graph://0d3f750f-f9c. Thin layer over ``settings_ops``.

The argparse setup lives in cli.py; per-subcommand handlers live here.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from . import ops
from .schemas.registry import SchemaValidationError


_VALID_PROMOTION_STATES = ("curated", "published", "canonical")


# ── helpers ──────────────────────────────────────────────────


def _read_payload_file(path: str) -> dict:
    """Load a payload from JSON or YAML. Falls back to JSON if PyYAML missing."""
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


# ── list / members / show ────────────────────────────────────


def _caller_org(args) -> str | None:
    return getattr(args, "caller_org", None)


def cmd_set_list(args) -> None:
    set_ids = ops.list_set_ids(caller_org=_caller_org(args))
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
    members = ops.read_set(
        args.set_id, target_revision=target, min_revision=minrev,
        caller_org=_caller_org(args),
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
            "state": m.publication_state,
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
        print(f"Dropped: {dict(members.dropped)}")


def cmd_set_show(args) -> None:
    target, _, _, no_upconvert = _resolve_read_flags(args)
    if no_upconvert:
        target = None
    got = ops.get_setting(
        args.id, target_revision=target, caller_org=_caller_org(args),
    )
    if got is None:
        print(f"Setting not found (or dropped by --as-rev): {args.id}",
              file=sys.stderr)
        sys.exit(1)
    out = {
        "id": got.id,
        "set_id": got.set_id,
        "stored_revision": got.stored_revision,
        "target_revision": got.target_revision,
        "key": got.key,
        "publication_state": got.publication_state,
        "supersedes": got.supersedes,
        "excludes": got.excludes,
        "deprecated": got.deprecated,
        "successor_id": got.successor_id,
        "created_at": got.created_at,
        "updated_at": got.updated_at,
        "payload": got.payload,
    }
    print(json.dumps(out, indent=2))


# ── add / override / exclude ────────────────────────────────


def cmd_set_add(args) -> None:
    set_id, rev = _parse_set_at_rev(args.set_at_rev)
    payload = _read_payload_file(args.from_file)
    try:
        sid = ops.add_setting(
            set_id, rev, args.key, payload, state=args.state,
            caller_org=_caller_org(args),
        )
    except SchemaValidationError as e:
        print(f"Error: schema validation failed: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"  ✓ Setting: {sid[:11]}  {set_id}#{rev}  key={args.key}  [{args.state}]")


def cmd_set_override(args) -> None:
    payload = _read_payload_file(args.from_file)
    try:
        sid = ops.override_setting(
            args.target_id, payload, state=args.state,
            caller_org=_caller_org(args),
        )
    except LookupError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except SchemaValidationError as e:
        print(f"Error: merged payload fails validation: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"  ✓ Override: {sid[:11]}  supersedes={args.target_id[:11]}  [{args.state}]")


def cmd_set_exclude(args) -> None:
    try:
        sid = ops.exclude_setting(
            args.target_id, state=args.state, caller_org=_caller_org(args),
        )
    except LookupError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"  ✓ Exclude: {sid[:11]}  excludes={args.target_id[:11]}  [{args.state}]")


# ── promote / deprecate / remove ────────────────────────────


def cmd_set_promote(args) -> None:
    if args.to not in _VALID_PROMOTION_STATES:
        print(
            f"Error: --to must be one of {_VALID_PROMOTION_STATES}, got {args.to!r}",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        ops.promote_setting(args.id, args.to, caller_org=_caller_org(args))
    except LookupError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"  ✓ Promoted: {args.id[:11]} → {args.to}")


def cmd_set_deprecate(args) -> None:
    try:
        ops.deprecate_setting(
            args.id, successor_id=args.successor,
            caller_org=_caller_org(args),
        )
    except LookupError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    suc = f" successor={args.successor[:11]}" if args.successor else ""
    print(f"  ✓ Deprecated: {args.id[:11]}{suc}")


def cmd_set_remove(args) -> None:
    try:
        ops.remove_setting(args.id, caller_org=_caller_org(args))
    except LookupError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"  ✓ Removed: {args.id[:11]}")


# ── migrate ─────────────────────────────────────────────────


def cmd_set_migrate(args) -> None:
    try:
        report = ops.migrate_setting_revisions(
            args.set_id, args.to_rev, dry_run=args.dry_run,
            caller_org=_caller_org(args),
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


def _add_caller_org_arg(parser) -> None:
    parser.add_argument(
        "--caller-org", dest="caller_org", default=None,
        help="Route to the per-org DB at data/orgs/<slug>.db "
             "(default: autonomy / GRAPH_DB env)",
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
    _add_caller_org_arg(p_list)
    p_list.set_defaults(func=cmd_set_list)

    # members
    p_members = set_sub.add_parser(
        "members", help="List resolved members of a SET (default: stored revisions)",
    )
    p_members.add_argument("set_id")
    add_read_flags(p_members)
    _add_caller_org_arg(p_members)
    p_members.set_defaults(func=cmd_set_members)

    # show
    p_show = set_sub.add_parser("show", help="Show a single Setting in detail")
    p_show.add_argument("id")
    add_read_flags(p_show)
    _add_caller_org_arg(p_show)
    p_show.set_defaults(func=cmd_set_show)

    # add
    p_add = set_sub.add_parser("add", help="Create a base Setting")
    p_add.add_argument("set_at_rev",
                       help="set_id#schema_revision, e.g. autonomy.workspace#1")
    p_add.add_argument("--key", required=True, help="Identity within (set_id, this DB)")
    p_add.add_argument("--from", dest="from_file", required=True,
                       help="Path to JSON or YAML payload file")
    p_add.add_argument("--state", default="raw",
                       choices=("raw", "curated", "published", "canonical"))
    _add_caller_org_arg(p_add)
    p_add.set_defaults(func=cmd_set_add)

    # override
    p_over = set_sub.add_parser("override", help="Create an override Setting")
    p_over.add_argument("target_id", help="Setting id being overridden")
    p_over.add_argument("--from", dest="from_file", required=True,
                        help="Path to partial JSON/YAML payload (merge-patch)")
    p_over.add_argument("--state", default="raw",
                        choices=("raw", "curated", "published", "canonical"))
    _add_caller_org_arg(p_over)
    p_over.set_defaults(func=cmd_set_override)

    # exclude
    p_excl = set_sub.add_parser("exclude", help="Create an exclude Setting")
    p_excl.add_argument("target_id", help="Setting id being excluded")
    p_excl.add_argument("--state", default="raw",
                        choices=("raw", "curated", "published", "canonical"))
    _add_caller_org_arg(p_excl)
    p_excl.set_defaults(func=cmd_set_exclude)

    # promote
    p_prom = set_sub.add_parser("promote", help="Transition publication_state")
    p_prom.add_argument("id")
    p_prom.add_argument("--to", required=True,
                        choices=_VALID_PROMOTION_STATES)
    _add_caller_org_arg(p_prom)
    p_prom.set_defaults(func=cmd_set_promote)

    # deprecate
    p_dep = set_sub.add_parser("deprecate", help="Mark a Setting deprecated")
    p_dep.add_argument("id")
    p_dep.add_argument("--successor", help="Optional successor Setting id")
    _add_caller_org_arg(p_dep)
    p_dep.set_defaults(func=cmd_set_deprecate)

    # remove
    p_rem = set_sub.add_parser("remove", help="Hard-delete a raw Setting")
    p_rem.add_argument("id")
    _add_caller_org_arg(p_rem)
    p_rem.set_defaults(func=cmd_set_remove)

    # migrate
    p_mig = set_sub.add_parser(
        "migrate", help="Rewrite stored rows up to a target revision",
    )
    p_mig.add_argument("set_id")
    p_mig.add_argument("--to-rev", type=int, required=True, dest="to_rev")
    p_mig.add_argument("--dry-run", action="store_true", dest="dry_run")
    _add_caller_org_arg(p_mig)
    p_mig.set_defaults(func=cmd_set_migrate)
