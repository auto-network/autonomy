"""AST conformance: no cmd_* function calls ops directly.

Every operation from a CLI handler must flow through ``get_client()``
(see tools/graph/client.py) so that containers routing via ``GRAPH_API``
never open local sqlite files.

Allowed exceptions:

* ``ops.CrossOrgWriteError`` — exception class identity, not an operation.
* Functions tagged with a leading ``# CLIENT_EXEMPT: <reason>`` comment on
  the line above the offending call.

This test lands RED on master when cmd_* functions still call ops.* in
their bodies; each migration lands another call site GREEN. When all
targets migrate the assertion passes with zero remaining violations.

Design reference: graph://bcce359d-a1d (Cross-Org Search Architecture).
"""

from __future__ import annotations

import ast
from pathlib import Path


_CLI_FILES = (
    Path(__file__).resolve().parents[1] / "cli.py",
    Path(__file__).resolve().parents[1] / "set_cmd.py",
)

# Attribute access whose ``value`` is ``Name('ops' | '_ops')`` counts as a
# direct ops call. Exception: the bare type reference ``CrossOrgWriteError``
# and its ``CrossOrgWriteResolved`` sibling — these are exception-class
# identities used in raise/except clauses, not operations.
_EXEMPT_ATTRS = frozenset({"CrossOrgWriteError", "CrossOrgWriteResolved"})


def _cmd_funcs(tree: ast.AST) -> list[ast.FunctionDef]:
    out: list[ast.FunctionDef] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("cmd_"):
            out.append(node)
    return out


def _ops_calls(fn: ast.FunctionDef, source_lines: list[str]) -> list[tuple[int, str]]:
    """Return every ``ops.<attr>`` or ``_ops.<attr>`` access inside ``fn``.

    Returns list of (lineno, attr).  Skips CrossOrgWriteError
    identity access and any call on a line preceded by an explicit
    ``# CLIENT_EXEMPT: <reason>`` marker.
    """
    hits: list[tuple[int, str]] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Attribute):
            continue
        if not isinstance(node.value, ast.Name):
            continue
        if node.value.id not in ("ops", "_ops"):
            continue
        if node.attr in _EXEMPT_ATTRS:
            continue
        lineno = node.lineno
        prev = source_lines[lineno - 2].strip() if lineno >= 2 else ""
        if prev.startswith("# CLIENT_EXEMPT:"):
            continue
        hits.append((lineno, node.attr))
    return hits


def test_cmd_functions_route_through_get_client():
    """Every cmd_* body must call ``get_client()``, not ``ops.X()`` directly.

    Land this test RED if violations remain. Each migration lands one
    call site GREEN.
    """
    violations: list[str] = []
    for path in _CLI_FILES:
        src = path.read_text()
        source_lines = src.splitlines()
        tree = ast.parse(src, filename=str(path))
        for fn in _cmd_funcs(tree):
            for lineno, attr in _ops_calls(fn, source_lines):
                violations.append(f"{path.name}:{lineno} {fn.name} → ops.{attr}")
    assert not violations, (
        "CMD_* must not call ops directly — route through get_client() instead. "
        "Add '# CLIENT_EXEMPT: <reason>' above the line only for genuinely host-only "
        f"operations. Violations:\n  " + "\n  ".join(violations)
    )


def test_cross_org_write_error_reference_allowed():
    """``except _ops.CrossOrgWriteError`` / ``raise ops.CrossOrgWriteError(...)``
    are exception-class identities and do NOT count as ops-operation calls."""
    # Construct a minimal cmd_* that catches the exception-class identity;
    # the AST walk should yield zero violations for it.
    sample = (
        "from . import ops as _ops\n"
        "def cmd_example():\n"
        "    try:\n"
        "        pass\n"
        "    except _ops.CrossOrgWriteError:\n"
        "        pass\n"
    )
    tree = ast.parse(sample)
    fn = tree.body[-1]
    assert isinstance(fn, ast.FunctionDef)
    hits = _ops_calls(fn, sample.splitlines())
    assert hits == []


def test_client_exempt_marker_skips_violation():
    """A ``# CLIENT_EXEMPT: <reason>`` comment on the preceding line marks
    a deliberate opt-in (e.g. host-only maintenance ops)."""
    sample = (
        "from . import ops as _ops\n"
        "def cmd_example():\n"
        "    # CLIENT_EXEMPT: host-only maintenance — never runs in container\n"
        "    _ops.dangerous_host_op()\n"
    )
    tree = ast.parse(sample)
    fn = tree.body[-1]
    assert isinstance(fn, ast.FunctionDef)
    hits = _ops_calls(fn, sample.splitlines())
    assert hits == []


def test_plain_ops_call_flagged():
    """Control test: a plain ops.X() call inside a cmd_ IS flagged."""
    sample = (
        "from . import ops as _ops\n"
        "def cmd_example():\n"
        "    _ops.list_sources()\n"
    )
    tree = ast.parse(sample)
    fn = tree.body[-1]
    assert isinstance(fn, ast.FunctionDef)
    hits = _ops_calls(fn, sample.splitlines())
    assert hits == [(3, "list_sources")]


def test_no_is_api_mode_in_cli():
    """``is_api_mode()`` is the old dual-path fork. The client decides
    now (``get_client()`` → ``HttpClient`` vs ``ops`` module). Any
    reappearance of ``is_api_mode`` in cli.py / set_cmd.py is a
    regression to the parallel-code-path architecture."""
    offenders: list[str] = []
    for path in _CLI_FILES:
        src = path.read_text()
        for lineno, line in enumerate(src.splitlines(), start=1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            if "is_api_mode" in line:
                offenders.append(f"{path.name}:{lineno}")
    assert not offenders, (
        "is_api_mode() must not reappear in cli.py / set_cmd.py — the "
        "client layer decides HTTP-vs-ops. Offending lines:\n  "
        + "\n  ".join(offenders)
    )


def test_no_api_client_import():
    """``tools/graph/api_client`` was deleted — nothing should import from it."""
    for path in _CLI_FILES:
        src = path.read_text()
        assert "from .api_client" not in src, f"{path.name} still imports api_client"
        assert "import api_client" not in src, f"{path.name} still imports api_client"


# ── GraphDB / db.X bypass detection ──────────────────────────────


def _is_host_only_guard(test: ast.expr) -> bool:
    """True if ``test`` establishes a host-only branch via some shape of
    ``not isinstance(<any>, HttpClient)`` — possibly combined with other
    conjuncts in an ``and`` expression.

    Accepted shapes:

    * ``not isinstance(client, HttpClient)``
    * ``not isinstance(x, HttpClient) and <other>`` (any conjunct form)
    * ``<other> and not isinstance(x, HttpClient)``
    """
    # Pure `not isinstance(...)` — simple guard.
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return _is_host_only_guard_inner(test.operand, inverted=True)
    # `A and B and …` — host-only if ANY conjunct is `not isinstance(..., HttpClient)`.
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And):
        return any(_is_host_only_guard(v) for v in test.values)
    return _is_host_only_guard_inner(test, inverted=False)


def _is_host_only_guard_inner(node: ast.expr, *, inverted: bool) -> bool:
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id == "isinstance" and len(node.args) == 2:
            target = node.args[1]
            if isinstance(target, ast.Name) and target.id == "HttpClient":
                # isinstance(client, HttpClient) → True when container.
                # host-only branch requires inversion (not isinstance ...).
                return inverted
    return False


def _enclosing_host_only(path: list[ast.AST]) -> bool:
    """True if any enclosing If/IfExp/else-of-If establishes a host-only
    branch and ``node`` is inside it."""
    for i, parent in enumerate(path):
        if isinstance(parent, ast.If):
            child = path[i + 1] if i + 1 < len(path) else None
            if child is None:
                continue
            if child in parent.body and _is_host_only_guard(parent.test):
                return True
            if child in parent.orelse:
                # else branch of `if isinstance(client, HttpClient):` is host-only.
                if isinstance(parent.test, ast.Call):
                    if (isinstance(parent.test.func, ast.Name)
                            and parent.test.func.id == "isinstance"
                            and len(parent.test.args) == 2
                            and isinstance(parent.test.args[1], ast.Name)
                            and parent.test.args[1].id == "HttpClient"):
                        return True
    return False


def _early_return_host_cutoff(block: list[ast.stmt]) -> int | None:
    """Return the line number at which ``block`` transitions to implicit
    host-only code.

    Scans top-level statements for *any* ``if isinstance(client, HttpClient):
    ...; return`` guard — even if earlier ``If`` statements (e.g. ``if
    args.status: ... return``) precede it. The first container guard that
    ends in ``return`` marks the cutoff; everything after is host-only.
    """
    for stmt in block:
        if not isinstance(stmt, ast.If):
            continue
        if not _is_container_guard(stmt.test):
            continue
        if not stmt.body:
            continue
        last = stmt.body[-1]
        if isinstance(last, ast.Return):
            end = stmt.end_lineno or stmt.lineno
            return end + 1
    return None


def _is_container_guard(test: ast.expr) -> bool:
    """True if ``test`` is ``isinstance(<any>, HttpClient)`` (non-inverted)."""
    if isinstance(test, ast.Call) and isinstance(test.func, ast.Name):
        if test.func.id == "isinstance" and len(test.args) == 2:
            target = test.args[1]
            if isinstance(target, ast.Name) and target.id == "HttpClient":
                return True
    return False


def _fn_references_client_layer(fn: ast.FunctionDef) -> bool:
    """True if the function's body mentions ``get_client`` or ``HttpClient``.

    A cmd_ that makes zero reference to the client layer is, by design, a
    host-only maintenance command (ingest, seed, local-filesystem
    scanning). The container never reaches them — the dispatcher doesn't
    expose those subcommands, and the CLI is invoked with GRAPH_API unset
    on the host. We don't demand get_client() plumbing for them.
    """
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and node.id in ("get_client", "HttpClient"):
            return True
    return False


def _graphdb_or_db_hits(
    fn: ast.FunctionDef, source_lines: list[str],
) -> list[tuple[int, str]]:
    """Return AST hits for ``GraphDB(...)`` / ``GraphDB.X(...)`` / ``db.X()``
    inside a ``cmd_*`` body that aren't inside a host-only region.

    Skipped:

    * Any function that never references ``get_client`` / ``HttpClient`` —
      pure host-only maintenance commands (ingest, seed, etc.).
    * Any line preceded by ``# CLIENT_EXEMPT: <reason>``.
    * Any node inside a ``if not isinstance(client, HttpClient):`` branch
      or the ``else`` branch of ``if isinstance(client, HttpClient):`` —
      inline host-only regions.
    * Any node that follows a top-level
      ``if isinstance(client, HttpClient): ...; return`` guard — the rest
      of the function is implicitly host-only after the container early
      returns.

    The three AST shapes flagged:

    1. ``Call(func=Name(id='GraphDB'))``           — direct constructor.
    2. ``Call(func=Attribute(value=Name('GraphDB')))`` — class-level call.
    3. Any attribute access on a local name ``db`` / ``own_db`` / ``peer_db``.
    """
    # Pure host-only command (no client abstraction in play): skip entirely.
    if not _fn_references_client_layer(fn):
        return []

    # Find the top-level early-return cutoff (if any).
    early_cutoff = _early_return_host_cutoff(fn.body)

    # Build a parent map so we can walk outward from a hit.
    parents: dict[int, ast.AST] = {}
    for parent in ast.walk(fn):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent

    def _ancestors(node: ast.AST) -> list[ast.AST]:
        chain: list[ast.AST] = []
        while id(node) in parents:
            node = parents[id(node)]
            chain.append(node)
        return chain

    hits: list[tuple[int, str]] = []
    for node in ast.walk(fn):
        snippet: str | None = None
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "GraphDB":
                snippet = "GraphDB(...)"
            elif (isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "GraphDB"):
                snippet = f"GraphDB.{func.attr}(...)"
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id in ("db", "own_db", "peer_db"):
                snippet = f"{node.value.id}.{node.attr}"
        if snippet is None:
            continue
        lineno = node.lineno
        prev = source_lines[lineno - 2].strip() if lineno >= 2 else ""
        if prev.startswith("# CLIENT_EXEMPT:"):
            continue
        # Top-level early-return cutoff: anything after it is host-only.
        if early_cutoff is not None and lineno >= early_cutoff:
            continue
        # Walk ancestors looking for an enclosing host-only If.
        host_only = False
        chain = _ancestors(node)
        for i, ancestor in enumerate(chain):
            if not isinstance(ancestor, ast.If):
                continue
            # Find the child from ancestor towards node.
            if i == 0:
                child = node
            else:
                child = chain[i - 1]
            if child in ancestor.body and _is_host_only_guard(ancestor.test):
                host_only = True
                break
            if child in ancestor.orelse:
                test = ancestor.test
                if (isinstance(test, ast.Call)
                        and isinstance(test.func, ast.Name)
                        and test.func.id == "isinstance"
                        and len(test.args) == 2
                        and isinstance(test.args[1], ast.Name)
                        and test.args[1].id == "HttpClient"):
                    host_only = True
                    break
        if host_only:
            continue
        hits.append((lineno, snippet))
    return hits


def test_cmd_functions_do_not_construct_graphdb_or_use_db_handle():
    """Every ``cmd_*`` body must route through ``get_client()``.

    Opening a ``GraphDB(...)`` handle inside a ``cmd_*`` — or calling
    methods on a local ``db`` / ``own_db`` / ``peer_db`` variable —
    bypasses the client layer and breaks the container path. Mark
    legitimate host-only maintenance call sites with
    ``# CLIENT_EXEMPT: <reason>`` on the line above.
    """
    violations: list[str] = []
    for path in _CLI_FILES:
        src = path.read_text()
        source_lines = src.splitlines()
        tree = ast.parse(src, filename=str(path))
        for fn in _cmd_funcs(tree):
            for lineno, attr in _graphdb_or_db_hits(fn, source_lines):
                violations.append(f"{path.name}:{lineno} {fn.name} → {attr}")
    assert not violations, (
        "CMD_* must not open GraphDB / call db.X() directly — route through "
        "get_client() instead. Mark genuinely host-only call sites with "
        "'# CLIENT_EXEMPT: <reason>' on the preceding line. Violations:\n  "
        + "\n  ".join(violations)
    )


def test_client_exempt_skips_graphdb_violation():
    """The CLIENT_EXEMPT marker also opts out of the GraphDB/db.X check."""
    sample = (
        "from .db import GraphDB\n"
        "def cmd_example():\n"
        "    # CLIENT_EXEMPT: host-only ingest — never runs in container\n"
        "    db = GraphDB('/tmp/x.db')\n"
        "    # CLIENT_EXEMPT: same\n"
        "    db.stats()\n"
    )
    tree = ast.parse(sample)
    fn = tree.body[-1]
    assert isinstance(fn, ast.FunctionDef)
    assert _graphdb_or_db_hits(fn, sample.splitlines()) == []
