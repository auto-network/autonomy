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
