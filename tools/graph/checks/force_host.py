"""Detect graph-CLI subprocess invocations that pass ``--db`` without
``--force-host``.

Background (auto-lq20j, auto-41haz)
-----------------------------------
Post auto-lq20j, ``tools.graph.client.get_client()`` defaults to
``HttpClient``. Any subprocess that runs ``python -m tools.graph.cli``
(or the ``graph`` entrypoint) without ``--force-host`` will route writes
through the live dashboard's API — *even when ``--db <tmp>`` is passed
on the same command line*. The ``--db`` flag is honoured only by
host-direct dispatch; in HTTP mode it is ignored, so the test's tmp DB
stays empty and the failure mode is invisible.

This check fails the test suite when a subprocess call site mixes
``--db`` with the graph CLI but omits ``--force-host``. The contract is
documented at graph://90e6fe6d-89c.

Detection model
---------------
For each subprocess call site (``subprocess.run`` / ``Popen`` / ``call``
/ ``check_call`` / ``check_output``):

1. Walk up to the enclosing function (or module) scope.
2. Collect every string literal that appears inside that scope —
   covers the common pattern of constructing the command list across
   conditional ``cmd.extend(["--db", str(db)])`` branches.
3. Decide whether this is a graph-CLI invocation:
   - the scope's string pool contains ``"tools.graph.cli"``, or
   - the call's first positional argument is a list/tuple whose first
     literal element is ``"graph"`` (the entrypoint binary), or
   - the call's first positional argument is a ``Name`` whose first
     literal assignment in the scope starts with ``"graph"``.
4. If the scope's string pool also contains ``"--db"`` but lacks
   ``"--force-host"``, record a :class:`Violation`.

The check is intentionally scope-local: graph CLI subprocesses are
always built and executed in the same helper function in our code, so
function-scoped string-pool inspection catches the dynamic
``cmd.extend(...)`` idiom without needing full data-flow analysis.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


_REPO_ROOT = Path(__file__).resolve().parents[3]

_SUBPROCESS_ATTRS = frozenset(
    {"run", "Popen", "call", "check_call", "check_output"}
)

_DEFAULT_SCAN_ROOTS: tuple[str, ...] = ("tools", "agents")

_VIOLATION_REASON = (
    "subprocess invocation of graph CLI passes '--db' but not "
    "'--force-host'; without --force-host the CLI defaults to HttpClient "
    "and silently routes writes to the live dashboard DB, ignoring --db. "
    "Contract: graph://90e6fe6d-89c."
)


@dataclass(frozen=True)
class Violation:
    """A site that needs ``--force-host`` added.

    ``path`` is whatever the caller passed (typically an absolute path
    on disk, or a placeholder like ``"<source>"`` for synthetic input).
    ``lineno`` is 1-based and points at the subprocess call.
    """

    path: str
    lineno: int
    reason: str

    def format(self, repo_root: Path | None = None) -> str:
        try:
            display = str(
                Path(self.path).resolve().relative_to(repo_root or _REPO_ROOT)
            )
        except (ValueError, OSError):
            display = self.path
        return f"{display}:{self.lineno}: {self.reason}"


def find_violations_in_source(source: str, path: str = "<source>") -> list[Violation]:
    """Scan a single Python source string and return any violations."""
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError:
        return []

    parents: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent

    def enclosing_scope(node: ast.AST) -> ast.AST:
        cur: ast.AST = node
        while id(cur) in parents:
            cur = parents[id(cur)]
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
                return cur
        return tree

    violations: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_subprocess_call(node):
            continue
        scope = enclosing_scope(node)
        pool = _string_pool(scope)
        if not _looks_like_graph_cli(node, scope, pool):
            continue
        if "--db" not in pool:
            continue
        if "--force-host" in pool:
            continue
        violations.append(Violation(path=path, lineno=node.lineno, reason=_VIOLATION_REASON))
    return violations


def find_violations_in_repo(
    roots: Iterable[Path] | None = None,
) -> list[Violation]:
    """Scan all ``.py`` files under the given roots.

    ``roots`` defaults to ``[REPO_ROOT/"tools", REPO_ROOT/"agents"]``.
    Hidden directories (``.git``, ``.venv``, etc.) and the ``tools/graph/checks``
    package itself are skipped — the check should not flag its own
    string-pool fixtures.
    """
    if roots is None:
        roots = [_REPO_ROOT / r for r in _DEFAULT_SCAN_ROOTS]

    violations: list[Violation] = []
    for root in roots:
        if not root.exists():
            continue
        for py in root.rglob("*.py"):
            if any(part.startswith(".") for part in py.parts):
                continue
            if "checks" in py.parts and "graph" in py.parts:
                # Skip this package's own modules and tests; they
                # contain --db / --force-host / "tools.graph.cli" string
                # literals as test fixtures and documentation, not as
                # actual subprocess invocations.
                continue
            try:
                source = py.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            violations.extend(find_violations_in_source(source, str(py)))
    return violations


# ── helpers ────────────────────────────────────────────────────────


def _is_subprocess_call(node: ast.Call) -> bool:
    f = node.func
    if not isinstance(f, ast.Attribute) or f.attr not in _SUBPROCESS_ATTRS:
        return False
    return isinstance(f.value, ast.Name) and f.value.id == "subprocess"


def _string_pool(scope: ast.AST) -> set[str]:
    return {
        n.value
        for n in ast.walk(scope)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }


def _looks_like_graph_cli(call: ast.Call, scope: ast.AST, pool: set[str]) -> bool:
    """True if this subprocess call targets the graph CLI."""
    if "tools.graph.cli" in pool:
        return True
    if not call.args:
        return False
    arg = call.args[0]
    if isinstance(arg, (ast.List, ast.Tuple)):
        return _list_starts_with_graph(arg)
    if isinstance(arg, ast.Name):
        for sub in ast.walk(scope):
            if not isinstance(sub, ast.Assign):
                continue
            if not any(
                isinstance(t, ast.Name) and t.id == arg.id for t in sub.targets
            ):
                continue
            if isinstance(sub.value, (ast.List, ast.Tuple)) and _list_starts_with_graph(
                sub.value
            ):
                return True
    return False


def _list_starts_with_graph(lst: ast.List | ast.Tuple) -> bool:
    if not lst.elts:
        return False
    first = lst.elts[0]
    return isinstance(first, ast.Constant) and first.value == "graph"
