"""Bounded write path for serve.auto.network DNS-01 challenges.

The zone's ONLY mutation surface (auto-g1jxw): TXT values at
``_acme-challenge.<label>.serve.auto.network``, name grammar validated
before any write, expiry clamped, per-name values capped in the store.
Simultaneous apex+wildcard values at one name coexist (each present is
an independent row). auto-bhs3c later fronts these same functions with
the authenticated ``serve:dns-01`` control op; this CLI is the on-box
administrative and demonstration path.

    python -m tools.network.registry.dns_challenges \
        [--db PATH] present <name> <value> [--expiry N]
    ... cleanup <name> <value>
Exit 0 on success, 2 on a refused request, 1 on store failure.
"""

from __future__ import annotations

import argparse
import re
import sys
import time

ZONE = "serve.auto.network"
CHALLENGE_PREFIX = "_acme-challenge."
DEFAULT_EXPIRY = 900
EXPIRY_FLOOR, EXPIRY_CEILING = 60, 3600
MAX_VALUE_LENGTH = 255

_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


class ChallengeError(Exception):
    """A request outside the challenge bounds — refused before any write."""


def validate_challenge_name(name: str) -> str:
    """Return the serving label of a valid challenge name, else refuse."""
    if not isinstance(name, str) or len(name) > 253:
        raise ChallengeError("challenge name is not a valid DNS name")
    suffix = "." + ZONE
    if not name.endswith(suffix):
        raise ChallengeError(f"challenge name must end with {suffix}")
    head = name[: -len(suffix)]
    if not head.startswith(CHALLENGE_PREFIX):
        raise ChallengeError(
            f"challenge name must start with {CHALLENGE_PREFIX}")
    label = head[len(CHALLENGE_PREFIX):]
    if "." in label or _LABEL_RE.match(label) is None:
        raise ChallengeError(
            "challenge name must be _acme-challenge.<label>." + ZONE)
    return label


def validate_value(value: str) -> str:
    if not isinstance(value, str) or not value \
            or len(value) > MAX_VALUE_LENGTH:
        raise ChallengeError("challenge value must be 1..255 characters")
    if '"' in value or "\\" in value or "\n" in value:
        raise ChallengeError("challenge value carries forbidden characters")
    return value


def present(store, name: str, value: str, *, expiry: int = DEFAULT_EXPIRY,
            now_fn=time.time) -> None:
    validate_challenge_name(name)
    validate_value(value)
    expiry = max(EXPIRY_FLOOR, min(EXPIRY_CEILING, int(expiry)))
    now = int(now_fn())
    try:
        store.upsert_serve_challenge(
            name + ".", value, expires_at=now + expiry, now=now)
    except ValueError as exc:
        raise ChallengeError(str(exc)) from exc


def cleanup(store, name: str, value: str) -> None:
    validate_challenge_name(name)
    store.delete_serve_challenge(name + ".", validate_value(value))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--db", default="/var/lib/autonomy-registry/registry.db")
    sub = parser.add_subparsers(dest="op", required=True)
    p = sub.add_parser("present")
    p.add_argument("name")
    p.add_argument("value")
    p.add_argument("--expiry", type=int, default=DEFAULT_EXPIRY)
    c = sub.add_parser("cleanup")
    c.add_argument("name")
    c.add_argument("value")
    args = parser.parse_args(argv)

    from .store import RegistryStore

    store = RegistryStore(args.db)
    try:
        if args.op == "present":
            present(store, args.name, args.value, expiry=args.expiry)
        else:
            cleanup(store, args.name, args.value)
    except ChallengeError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
