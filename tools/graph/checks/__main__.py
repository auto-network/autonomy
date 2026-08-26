"""CLI entrypoint: ``python -m tools.graph.checks``.

Runs every static check in this package and exits non-zero on any
violation. Suitable for pre-commit hooks or CI.

Current checks:

* :mod:`tools.graph.checks.force_host` — graph-CLI subprocess
  invocations missing ``--force-host``.
* :mod:`tools.graph.checks.settings_request_org` — request handlers
  passing the raw result of
  ``api_auth.organization_scope_from_request(request)`` into the Settings
  public API without the ``CALLER_ORG`` fallback.
"""

from __future__ import annotations

import sys
from pathlib import Path

from tools.graph.checks.force_host import (
    find_violations_in_repo as find_force_host_violations,
)
from tools.graph.checks.settings_request_org import (
    find_violations_in_repo as find_settings_request_org_violations,
)


def main(argv: list[str] | None = None) -> int:
    repo_root = Path(__file__).resolve().parents[3]
    roots = [repo_root / "tools", repo_root / "agents"]

    exit_code = 0

    force_host_violations = find_force_host_violations(roots)
    if force_host_violations:
        for v in force_host_violations:
            print(v.format(repo_root), file=sys.stderr)
        print(
            f"\n{len(force_host_violations)} force-host violation(s). Add "
            f"'--force-host' alongside '--db' on each call. Without it the "
            f"CLI defaults to HttpClient and silently routes the write to "
            f"the live dashboard.",
            file=sys.stderr,
        )
        exit_code = 1
    else:
        print("OK: no graph-CLI subprocess invocations missing --force-host")

    settings_violations = find_settings_request_org_violations(roots)
    if settings_violations:
        for v in settings_violations:
            print(v.format(repo_root), file=sys.stderr)
        print(
            f"\n{len(settings_violations)} settings-request-org violation(s). "
            f"Use 'org=org or graph_ops.CALLER_ORG' or replace "
            f"'api_auth.organization_scope_from_request(request)' with 'api_auth.settings_scope_from_request(request)'.",
            file=sys.stderr,
        )
        exit_code = 1
    else:
        print(
            "OK: no request handlers passing api_auth.organization_scope_from_request(request) directly "
            "into the Settings public API"
        )

    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
