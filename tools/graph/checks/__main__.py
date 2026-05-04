"""CLI entrypoint: ``python -m tools.graph.checks``.

Scans the repo for graph-CLI subprocess invocations missing
``--force-host`` and exits non-zero on any violation. Suitable for
pre-commit hooks or CI.
"""

from __future__ import annotations

import sys
from pathlib import Path

from tools.graph.checks.force_host import find_violations_in_repo


def main(argv: list[str] | None = None) -> int:
    repo_root = Path(__file__).resolve().parents[3]
    roots = [repo_root / "tools", repo_root / "agents"]
    violations = find_violations_in_repo(roots)
    if not violations:
        print("OK: no graph-CLI subprocess invocations missing --force-host")
        return 0
    for v in violations:
        print(v.format(repo_root), file=sys.stderr)
    print(
        f"\n{len(violations)} violation(s). Add '--force-host' alongside "
        f"'--db' on each call. Without it the CLI defaults to HttpClient "
        f"and silently routes the write to the live dashboard.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
