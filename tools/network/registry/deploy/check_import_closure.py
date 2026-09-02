#!/usr/bin/env python3
"""auto-493mx: fail a deploy BEFORE restart if the shipped tree cannot even
load the process's entry points.

The relay-hil-1 incident: a shipped module imported ``tools.network.clock`` at
module-load time, ``clock.py`` (a top-level sibling, not in the synced package
dirs) was absent, and the service crashed at startup with ModuleNotFoundError
— but only AFTER systemd had restarted it, so a successful-looking deploy took
the service down.

Rather than re-implement Python's import resolver (this tree uses relative
imports and PEP 420 namespace packages, which a naive AST walk cannot follow),
this uses Python's OWN import machinery: it imports each entry module under the
deployed root in this throwaway process. Importing a module runs its top-level
code — its whole import closure — but NOT its ``if __name__ == '__main__'``
block, so no server starts. Any ModuleNotFoundError is exactly the missing-file
class this guards; a ``tools.*`` miss is our bug, a third-party miss is a
missing pip dependency (also a real deploy failure, reported distinctly).

Run it on the TARGET with the deployed venv so third-party deps resolve:
    venv/bin/python deploy/check_import_closure.py /opt/autonomy-registry
Default entry points are the registry server and the DNS server.
"""

from __future__ import annotations

import importlib
import sys

DEFAULT_ENTRIES = (
    "tools.network.registry.__main__",
    "tools.network.registry.dns_server",
)


def check(root: str, entries) -> list[tuple[str, str, bool]]:
    """Import each entry under *root*. Returns a list of
    (entry, missing_module, is_first_party) for every entry that failed to
    load; empty when the whole closure resolves."""
    if root not in sys.path:
        sys.path.insert(0, root)
    failures: list[tuple[str, str, bool]] = []
    for entry in entries:
        try:
            importlib.import_module(entry)
        except ModuleNotFoundError as exc:
            name = exc.name or str(exc)
            failures.append((entry, name, name.startswith("tools.")))
        except ImportError as exc:  # e.g. a missing name in a present module
            name = getattr(exc, "name", None) or str(exc)
            failures.append((entry, name, str(name).startswith("tools.")))
    return failures


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: check_import_closure.py <deployed-root> [entry ...]",
              file=sys.stderr)
        return 2
    root = sys.argv[1]
    entries = sys.argv[2:] or list(DEFAULT_ENTRIES)
    failures = check(root, entries)
    if not failures:
        print(f"import-closure OK: entry points {list(entries)} load-resolve "
              f"within {root}")
        return 0
    first_party = [f for f in failures if f[2]]
    third_party = [f for f in failures if not f[2]]
    if first_party:
        print("DEPLOY BLOCKED: shipped tree is missing an in-repo module its "
              "entry points import at load time (the clock.py crash-on-startup "
              "class):", file=sys.stderr)
        for entry, missing, _ in first_party:
            print(f"  x {entry} -> ModuleNotFoundError: {missing}",
                  file=sys.stderr)
    if third_party:
        print("DEPLOY BLOCKED: a third-party dependency is not installed in "
              "the deployed environment:", file=sys.stderr)
        for entry, missing, _ in third_party:
            print(f"  x {entry} -> ModuleNotFoundError: {missing} "
                  "(pip install it into the venv)", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
