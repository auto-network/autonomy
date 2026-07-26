"""``graph org`` subcommand group — org registry CLI.

Spec: graph://d970d946-f95. Thin layer over :mod:`tools.graph.org_ops`.
The argparse setup is wired into ``cli.py`` via :func:`attach_org_subparser`.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from . import org_ops
from .org_ops import (
    OrgError, OrgExistsError, OrgNotFoundError, OrgReferencedError,
    VALID_ORG_TYPES,
)


# ── helpers ──────────────────────────────────────────────────


def _read_payload_file(path: str) -> dict:
    """Load a JSON or YAML object from disk.

    Mirrors set_cmd._read_payload_file so the seed-identity flag on
    ``graph org create`` accepts the same file shapes as ``graph set add``.
    """
    text = Path(path).read_text()
    p = path.lower()
    if p.endswith((".yaml", ".yml")):
        try:
            import yaml  # type: ignore[import-not-found]
        except ImportError:
            print(
                "Error: PyYAML not installed; pass a .json file or install pyyaml.",
                file=sys.stderr,
            )
            sys.exit(1)
        data = yaml.safe_load(text)
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            print(f"Error: invalid JSON in {path}: {e}", file=sys.stderr)
            sys.exit(1)
    if not isinstance(data, dict):
        print(
            f"Error: {path} must contain a JSON/YAML object",
            file=sys.stderr,
        )
        sys.exit(1)
    return data


def _print_table(rows: list[dict], cols: list[tuple[str, str, int]]) -> None:
    header = "  ".join(f"{h:<{w}}" for _, h, w in cols)
    sep = "  ".join("─" * w for _, _, w in cols)
    print(header)
    print(sep)
    for r in rows:
        print(
            "  ".join(
                f"{str(r.get(k, '') or '')[:w]:<{w}}" for k, _, w in cols
            )
        )


# ── commands ────────────────────────────────────────────────


def cmd_org_list(args) -> None:
    orgs = org_ops.list_orgs()
    if not orgs:
        print("(no orgs)")
        return
    rows = []
    for o in orgs:
        identity = org_ops.show_org(o.slug)
        name = ""
        if identity and identity.get("identity"):
            name = identity["identity"].get("payload", {}).get("name", "") or ""
        rows.append({
            "slug": o.slug,
            "type": o.type,
            "name": name,
            "created_at": (o.created_at or "")[:19],
            "id": o.id[:11],
        })
    _print_table(rows, [
        ("slug", "SLUG", 16),
        ("type", "TYPE", 9),
        ("name", "NAME", 28),
        ("created_at", "CREATED", 19),
        ("id", "ID", 12),
    ])


def cmd_org_show(args) -> None:
    detail = org_ops.show_org(args.slug)
    if detail is None:
        print(f"Error: org not found: {args.slug}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(detail, indent=2))


def _read_personal_password(args) -> str | None:
    """``--password-stdin`` / ``--password-fd`` / AUTONOMY_PERSONAL_PASSWORD."""
    if getattr(args, "password_stdin", False):
        return sys.stdin.readline().rstrip("\n")
    fd = getattr(args, "password_fd", None)
    if fd is not None:
        with os.fdopen(int(fd), "r", closefd=True) as handle:
            return handle.readline().rstrip("\n")
    return os.environ.get("AUTONOMY_PERSONAL_PASSWORD")


def cmd_org_create(args) -> None:
    identity_payload = None
    if args.identity:
        identity_payload = _read_payload_file(args.identity)
    password = _read_personal_password(args)
    if not password:
        # Creation IS the founding ceremony (auto-nixfv): the personal
        # password authorizes founding and seals the org key. There is no
        # unfounded creation path from the CLI.
        print(
            "Error: the personal-identity password is required — pass "
            "--password-stdin, --password-fd <n>, or set "
            "AUTONOMY_PERSONAL_PASSWORD (creation founds the ledger and "
            "seals the org key)",
            file=sys.stderr,
        )
        sys.exit(2)
    from tools.network.idkit.armor import ArmorPassphraseError

    try:
        result = org_ops.create_org_with_identity(
            args.slug,
            password,
            type_=args.type,
            identity_payload=identity_payload,
        )
    except ArmorPassphraseError:
        print(
            "Error: the personal password is incorrect; nothing was created",
            file=sys.stderr,
        )
        sys.exit(2)
    except OrgExistsError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except OrgError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)
    print(json.dumps(result.to_dict(), indent=2))
    ref = result.org
    print(
        f"  ✓ Org founded: {ref.slug}  id={ref.id[:11]}  "
        f"genesis={result.genesis_id[:12]}  root={result.root_pub[:12]}"
    )


def cmd_org_retrofit_ledgers(args) -> None:
    password = _read_personal_password(args)
    if not password:
        print(
            "Error: the personal-identity password is required — pass "
            "--password-stdin, --password-fd <n>, or set "
            "AUTONOMY_PERSONAL_PASSWORD",
            file=sys.stderr,
        )
        sys.exit(2)
    from tools.network.idkit.armor import ArmorPassphraseError

    try:
        report = org_ops.retrofit_found_ledgers(password)
    except ArmorPassphraseError:
        print(
            "Error: the personal password is incorrect; nothing was founded",
            file=sys.stderr,
        )
        sys.exit(2)
    except OrgError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)
    for entry in report.outcomes:
        genesis = f"  genesis={entry['genesis_id'][:12]}" if entry["genesis_id"] else ""
        print(f"  {entry['slug']}: {entry['outcome']}{genesis}")


def cmd_org_rename(args) -> None:
    try:
        report = org_ops.rename_org(args.slug, args.new_slug)
    except OrgNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except OrgExistsError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except OrgError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)
    print(
        f"  ✓ Renamed: {report.old_slug} → {report.new_slug}  "
        f"id={report.org.id[:11]}  ({len(report.rewrites)} reference(s) rewritten)"
    )
    for ref in report.rewrites:
        print(
            f"    rewrote {ref.org}/{ref.set_id} key={ref.key} "
            f"reason={ref.reason}"
        )


def cmd_org_remove(args) -> None:
    try:
        report = org_ops.remove_org(args.slug, force=bool(args.force))
    except OrgNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except OrgReferencedError as e:
        print(
            f"Error: cannot remove {e.slug}: "
            f"{len(e.references)} cross-DB reference(s)",
            file=sys.stderr,
        )
        for ref in e.references:
            print(
                f"  {ref.org}/{ref.set_id} key={ref.key} reason={ref.reason}",
                file=sys.stderr,
            )
        print("Re-run with --force to delete anyway.", file=sys.stderr)
        sys.exit(1)
    except OrgError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)
    if report.references:
        print(
            f"  ✓ Removed: {report.slug}  "
            f"({len(report.references)} reference(s) orphaned)"
        )
        for ref in report.references:
            print(
                f"    orphaned: {ref.org}/{ref.set_id} key={ref.key} "
                f"reason={ref.reason}"
            )
    else:
        print(f"  ✓ Removed: {report.slug}")


# ── argparse setup ──────────────────────────────────────────


def attach_org_subparser(sub) -> None:
    """Wire up ``graph org ...`` subcommands."""
    p_org = sub.add_parser(
        "org", help="Org registry: per-org DB management (graph://d970d946-f95)",
    )
    org_sub = p_org.add_subparsers(dest="org_subcmd", required=True)

    # list
    p_list = org_sub.add_parser("list", help="Enumerate orgs")
    p_list.set_defaults(func=cmd_org_list)

    # show
    p_show = org_sub.add_parser(
        "show", help="Bootstrap row + autonomy.org#1 Setting for one org",
    )
    p_show.add_argument("slug")
    p_show.set_defaults(func=cmd_org_show)

    # create
    p_create = org_sub.add_parser("create", help="Create a new org DB")
    p_create.add_argument("slug")
    p_create.add_argument(
        "--type", default="shared", choices=VALID_ORG_TYPES,
        help="Org type (default: shared)",
    )
    p_create.add_argument(
        "--identity",
        help="Optional path to JSON/YAML payload to seed autonomy.org#1",
    )
    p_create.add_argument(
        "--password-stdin", action="store_true",
        help="Read the personal-identity password from stdin (first line). "
             "Creation is the founding ceremony; the password is required "
             "(or --password-fd / AUTONOMY_PERSONAL_PASSWORD).",
    )
    p_create.add_argument(
        "--password-fd", type=int, default=None,
        help="Read the personal-identity password from this file descriptor.",
    )
    p_create.set_defaults(func=cmd_org_create)

    # retrofit-ledgers
    p_retrofit = org_sub.add_parser(
        "retrofit-ledgers",
        help="Found the authority ledger for every org that lacks one "
             "(one-time, idempotent; keyless orgs get a sealed root first)",
    )
    p_retrofit.add_argument(
        "--password-stdin", action="store_true",
        help="Read the personal-identity password from stdin (first line).",
    )
    p_retrofit.add_argument(
        "--password-fd", type=int, default=None,
        help="Read the personal-identity password from this file descriptor.",
    )
    p_retrofit.set_defaults(func=cmd_org_retrofit_ledgers)

    # rename
    p_rename = org_sub.add_parser(
        "rename", help="Rename an org (move DB file + rewrite references)",
    )
    p_rename.add_argument("slug")
    p_rename.add_argument("new_slug")
    p_rename.set_defaults(func=cmd_org_rename)

    # remove
    p_remove = org_sub.add_parser(
        "remove", help="Delete an org's DB (refuses if Settings reference it)",
    )
    p_remove.add_argument("slug")
    p_remove.add_argument(
        "--force", action="store_true",
        help="Delete even when peer-org Settings reference the slug",
    )
    p_remove.set_defaults(func=cmd_org_remove)
