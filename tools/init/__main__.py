"""CLI for first-run initialization: ``python -m tools.init``.

Examples::

    python -m tools.init                          # defaults (first org: autonomy)
    python -m tools.init --org acme --org-name "Acme Corp"
    python -m tools.init --no-tls --json

Exit code 0 on success (including the already-initialized no-op case).
"""

from __future__ import annotations

import argparse
import os
import json
import sys

from .first_run import initialize


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools.init",
        description=(
            "Idempotent first-run initialization: data dirs, empty schema'd "
            "personal + first org databases, default Settings, TLS keypair."
        ),
    )
    parser.add_argument(
        "--org", metavar="SLUG",
        help="slug of the first shared org (default: $AUTONOMY_FIRST_ORG or 'autonomy')",
    )
    parser.add_argument(
        "--org-name", metavar="NAME",
        help="display name of the first org (default: title-cased slug)",
    )
    parser.add_argument(
        "--invite", metavar="CODE",
        help="join an EXISTING org with this invitation code "
             "(default: $AUTONOMY_INVITE); mutually exclusive with --org and --fleet-invite",
    )
    parser.add_argument(
        "--fleet-invite", metavar="CODE",
        help="join an EXISTING personal Fleet with this invitation code "
             "(default: $AUTONOMY_FLEET_INVITE); mutually exclusive with --org and --invite",
    )
    parser.add_argument(
        "--root", metavar="PATH",
        help="deployment root (default: this checkout)",
    )
    parser.add_argument(
        "--no-tls", action="store_true",
        help="skip self-signed TLS keypair generation",
    )
    parser.add_argument(
        "--tls-domain", metavar="DOMAIN",
        help="CN/SAN for the self-signed cert (default: $DASHBOARD_DOMAIN or hostname)",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit the report as JSON",
    )
    args = parser.parse_args(argv)

    report = initialize(
        args.root,
        first_org=args.org,
        first_org_name=args.org_name,
        invite=args.invite or os.environ.get("AUTONOMY_INVITE"),
        fleet_invite=(
            args.fleet_invite or os.environ.get("AUTONOMY_FLEET_INVITE")
        ),
        tls=not args.no_tls,
        tls_domain=args.tls_domain,
    )

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return 0

    width = max(len(s.name) for s in report.steps)
    for step in report.steps:
        line = f"  {step.name:<{width}}  {step.action:<8}"
        if step.detail:
            line += f"  {step.detail}"
        print(line)
    if report.changed:
        print(f"\ninitialized deployment at {report.root}")
    else:
        print(f"\nalready initialized — no changes ({report.root})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
