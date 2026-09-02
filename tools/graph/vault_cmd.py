"""The ``graph vault`` verb layer over the two credential tiers.

One command shape drives both ``autonomy.vault.audited`` and
``autonomy.vault.secured``: the tier changes GATING, never SHAPE. The caller
never types a set_id or a policy class — ``seal`` resolves the destination from
scope and tier, and ``read``/``remove``/``list`` are tier-transparent, locating a
name across the two sets. The secret never crosses argv or an environment value.
"""

from __future__ import annotations

import sys

from .client import get_client
from .ops import CALLER_ORG
from .schemas.vault_credential import (
    VAULT_AUDITED_SET_ID,
    VAULT_CREDENTIAL_REVISION,
    VAULT_SECURED_SET_ID,
)
from .set_cmd import _read_secret_bytes

#: tier -> its set id, the only place the mapping lives.
_TIER_SET = {
    "secured": VAULT_SECURED_SET_ID,
    "audited": VAULT_AUDITED_SET_ID,
}
_SET_TIER = {v: k for k, v in _TIER_SET.items()}


def _vault_org(args):
    """Resolve the scope to a client ``org``.

    ``--personal`` (the default) targets the operator's own store; ``--org`` with
    no slug uses this session's bearer org; ``--org SLUG`` names one. The server
    derives the writeback namespace from the bearer, so a plain slug is only a
    label here — never an explicit key prefix.
    """
    scope_org = getattr(args, "org", None)
    if scope_org is None:
        return "personal"
    if scope_org is _ORG_SENTINEL:
        return CALLER_ORG
    return scope_org


#: ``--org`` given with no slug resolves to the caller's bearer org.
_ORG_SENTINEL = object()


def _add_scope(parser) -> None:
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument(
        "--personal", dest="org", action="store_const", const=None,
        help="the operator's own store (the default)",
    )
    scope.add_argument(
        "--org", dest="org", nargs="?", const=_ORG_SENTINEL, metavar="SLUG",
        help="this session's organization (org-isolated), or a named slug",
    )
    parser.set_defaults(org=None)


def _find_member(client, name, org):
    """The (tier, member) a name resolves to across both tiers, or (None, None)."""
    for tier, set_id in _TIER_SET.items():
        try:
            members = client.read_set(set_id, org=org)
        except Exception:  # noqa: BLE001 — a set the caller cannot read is "absent here"
            continue
        for m in members.members:
            if m.key == name:
                return tier, m
    return None, None


def _read_secret_text(args) -> str:
    secret = _read_secret_bytes(args)
    try:
        if not secret:
            print("Error: refusing to vault an empty secret", file=sys.stderr)
            sys.exit(1)
        try:
            return secret.decode("utf-8")
        except UnicodeDecodeError:
            print(
                "Error: vault secrets are UTF-8 text; encode binary material "
                "before sealing",
                file=sys.stderr,
            )
            sys.exit(1)
    finally:
        secret[:] = b"\x00" * len(secret)


def cmd_vault_seal(args) -> None:
    if ":" in args.name:
        print(
            "Error: the organization namespace is derived from your session; "
            "do not put an explicit prefix in the name",
            file=sys.stderr,
        )
        sys.exit(1)
    if args.audience is not None and args.tier == "audited":
        print(
            "Error: --to names an audience, which only a secured secret has; an "
            "audited secret releases to the delegate, not a policy class",
            file=sys.stderr,
        )
        sys.exit(1)

    org = _vault_org(args)
    client = get_client()

    # Tier is immutable per name: refuse a name already at the other tier unless
    # --retier moves it, so a forgetful write cannot silently downgrade.
    other_tier, other_member = _find_member(client, args.name, org)
    if other_member is not None and other_tier != args.tier:
        if not args.retier:
            print(
                f"Error: {args.name!r} already exists at the {other_tier} tier. "
                f"Re-run with --retier to move it to {args.tier}.",
                file=sys.stderr,
            )
            sys.exit(1)
        client.remove_vault_credential(_TIER_SET[other_tier], args.name, org=org)
        print(
            f"  ⚠ --retier: removed {args.name!r} from the {other_tier} tier",
            file=sys.stderr,
        )

    value = _read_secret_text(args)

    if args.tier == "secured":
        policy_class = args.audience or "personal-root"
        sid = client.seal_personal_setting(
            args.name, value, policy_class_id=policy_class,
        )
    else:
        sid = client.add_setting(
            VAULT_AUDITED_SET_ID, VAULT_CREDENTIAL_REVISION,
            args.name, {"value": value}, org=org,
        )
    ident = sid[:11] if isinstance(sid, str) else str(sid)
    print(f"  ✓ {args.tier} secret sealed: {args.name}  ({ident})")


def cmd_vault_read(args) -> None:
    org = _vault_org(args)
    client = get_client()
    tier, member = _find_member(client, args.name, org)
    if member is None:
        print(f"Error: no vault secret named {args.name!r}", file=sys.stderr)
        sys.exit(1)

    if tier == "audited":
        # Audited releases unattended: read_set already opened it with the warm
        # delegate, or failed closed.
        if member.vault_error is not None:
            print(f"Error: {member.vault_error.message}", file=sys.stderr)
            sys.exit(1)
        _emit_release(client, _TIER_SET[tier], args.name, org, member)
        return

    # Secured: the human-factor release rendezvous.
    opener = getattr(client, "request_vault_open", None)
    if opener is None:
        print(
            "Error: secured secrets require dashboard approval; retry without "
            "--force-host",
            file=sys.stderr,
        )
        sys.exit(1)
    receipt = opener(
        _TIER_SET[tier], args.name, org=org,
        ttl_seconds=getattr(args, "wait", 0) or 0,
    )
    print(receipt["path"])


def _emit_release(client, set_id, name, org, member):
    """A read result is a delivered release: print only the ramfs path, never the
    value, so the plaintext never enters the transcript."""
    sealed = getattr(member, "sealed_content_key", None)
    opener = getattr(client, "request_vault_open", None)
    if sealed is not None and opener is not None:
        print(opener(set_id, name, org=org)["path"])
        return
    # Direct-host recovery mode returns the opened payload inline; there is no
    # ramfs rendezvous to route it through.
    payload = getattr(member, "payload", None)
    if isinstance(payload, dict) and "value" in payload:
        print(payload["value"])
        return
    print(f"Error: {name!r} did not open", file=sys.stderr)
    sys.exit(1)


def cmd_vault_remove(args) -> None:
    org = _vault_org(args)
    client = get_client()
    tier, member = _find_member(client, args.name, org)
    if member is None:
        print(f"Error: no vault secret named {args.name!r}", file=sys.stderr)
        sys.exit(1)
    client.remove_vault_credential(_TIER_SET[tier], args.name, org=org)
    print(f"  ✓ removed {args.name}  ({tier})")


def cmd_vault_list(args) -> None:
    org = _vault_org(args)
    client = get_client()
    rows = []
    for tier, set_id in _TIER_SET.items():
        try:
            members = client.read_set(set_id, org=org)
        except Exception:  # noqa: BLE001
            continue
        for m in members.members:
            rows.append((m.key, tier))
    if not rows:
        print("(no vault secrets in this scope)")
        return
    width = max(len(name) for name, _ in rows)
    for name, tier in sorted(rows):
        print(f"  {name:<{width}}  {tier}")


def attach_vault_subparser(sub) -> None:
    """Wire up ``graph vault ...`` onto an existing subparsers object."""
    p_vault = sub.add_parser(
        "vault",
        help="Credentials: one verb layer over the audited and secured tiers",
    )
    vault_sub = p_vault.add_subparsers(dest="vault_subcmd", required=True)

    p_seal = vault_sub.add_parser(
        "seal",
        help="Seal a secret; the tier is a single flag, the secret never on argv",
    )
    p_seal.add_argument("name", help="Stable credential name (no org prefix)")
    _add_scope(p_seal)
    p_seal.add_argument(
        "--tier", choices=("secured", "audited"), default="secured",
        help="secured (human factor) or audited (unattended). Default: secured",
    )
    p_seal.add_argument(
        "--to", dest="audience", default=None, metavar="CLASS",
        help="audience policy class (secured only). Default: personal-root",
    )
    p_seal.add_argument(
        "--retier", action="store_true",
        help="move a name that exists at the other tier (a loud remove+seal)",
    )
    secret = p_seal.add_mutually_exclusive_group()
    secret.add_argument(
        "--from-file", dest="secret_file", metavar="PATH",
        help="read exact bytes from PATH ('-' means stdin)",
    )
    secret.add_argument(
        "--from-fd", dest="secret_fd", type=int, metavar="N",
        help="read exact bytes from an already-open file descriptor",
    )
    secret.add_argument(
        "--prompt", dest="secret_prompt", action="store_true",
        help="read one hidden line interactively",
    )
    p_seal.set_defaults(func=cmd_vault_seal)

    p_read = vault_sub.add_parser(
        "read", help="Release a secret to /run/secrets/<name> (path only, never the value)",
    )
    p_read.add_argument("name")
    _add_scope(p_read)
    p_read.add_argument(
        "--wait", type=int, default=0, metavar="N",
        help="opt-in bounded wait (seconds) for a secured approval",
    )
    p_read.set_defaults(func=cmd_vault_read)

    p_remove = vault_sub.add_parser("remove", help="Remove a secret by name (either tier)")
    p_remove.add_argument("name")
    _add_scope(p_remove)
    p_remove.set_defaults(func=cmd_vault_remove)

    p_list = vault_sub.add_parser(
        "list", help="List names, tier, and scope — never values",
    )
    _add_scope(p_list)
    p_list.set_defaults(func=cmd_vault_list)
