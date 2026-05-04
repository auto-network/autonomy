"""Detect request handlers that pass ``_caller_org(request)`` directly into
the Settings public API without the ``CALLER_ORG`` fallback.

Background (auto-cfb8u, auto-ryrfg)
-----------------------------------
Post auto-cfb8u, the Settings public API on ``settings_ops`` (and its
``graph_ops`` re-exports) requires ``org=`` as a keyword-only argument.
Three values are meaningful:

* an org slug → that org's DB
* :data:`graph_ops.CALLER_ORG` → env-cascade resolver (contextvar →
  ``GRAPH_ORG`` env → scopeless default)
* literal ``None`` → explicit *scopeless* write

In dashboard request handlers, ``_caller_org(request)`` returns
``X-Graph-Org`` header value or ``None``. Passing that bare result into
``settings_ops.add_setting(..., org=org)`` re-introduces the very bug
required-org was meant to prevent: a missing header silently lands the
write in the scopeless DB, while readers carrying ``X-Graph-Org`` see
nothing (graph://53f7412f-51e). Handlers must use either:

1. the inline fallback ``org=org or graph_ops.CALLER_ORG``, or
2. the ``_settings_caller_org(request)`` helper, which already returns
   ``CALLER_ORG`` on a missing header.

Detection model
---------------
For each function whose parameters include ``request``:

1. Walk every assignment in the function body. A name is *tainted* if
   its most recent assignment is a bare call ``_caller_org(<arg>)`` —
   no ``or graph_ops.CALLER_ORG`` fallback. A name is untainted if its
   most recent assignment is anything else (e.g. a fallback expression,
   ``_settings_caller_org(request)``, a literal slug, or a helper).
2. Walk every ``Call`` whose ``func`` resolves to ``settings_ops.X`` or
   ``graph_ops.X`` where ``X`` is in :data:`SETTINGS_API_FUNCS`.
3. Inspect the call's ``org=`` keyword:

   - ``Name`` whose id is currently tainted → :class:`Violation`.
   - inline ``Call(_caller_org, ...)`` without an ``or CALLER_ORG``
     fallback → :class:`Violation`.
   - everything else (literals, ``_settings_caller_org(...)`` calls,
     ``BoolOp(Or, [..., CALLER_ORG])``, ``IfExp`` with a CALLER_ORG
     fallback, helper return values, etc.) → clean.

The detector is intentionally scope-local: dashboard handlers always
build the org variable in the same function that issues the Settings
call, so per-function taint tracking catches the documented bad
patterns without a full data-flow analysis. Helpers that internally
call ``settings_ops._resolve_settings_caller(org)`` or
``org or graph_ops.CALLER_ORG`` are not handlers; they take ``request``
indirectly (via the ``org`` parameter, not ``request``) and are
correctly excluded.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


_REPO_ROOT = Path(__file__).resolve().parents[3]

SETTINGS_API_FUNCS = frozenset(
    {
        "add_setting",
        "upsert_by_key",
        "override_setting",
        "exclude_setting",
        "promote_setting",
        "deprecate_setting",
        "remove_setting",
        "remove_settings_by_key_prefix",
        "list_set_ids",
        "get_setting",
        "read_set",
        "migrate_setting_revisions",
        "resolve_setting_strict",
        "resolve_set_key",
        "chain_setting",
    }
)

_SETTINGS_MODULE_NAMES = frozenset({"settings_ops", "graph_ops"})

_CALLER_ORG_NAME = "_caller_org"

_DEFAULT_SCAN_ROOTS: tuple[str, ...] = ("tools", "agents")

_VIOLATION_REASON = (
    "request handler passes the raw result of {origin} to "
    "{module}.{func}(org=...) — without 'or graph_ops.CALLER_ORG' a "
    "missing X-Graph-Org header silently lands the write in the "
    "scopeless DB. Use 'org=org or graph_ops.CALLER_ORG' or replace "
    "'_caller_org(request)' with '_settings_caller_org(request)'. "
    "Contract: graph://53f7412f-51e (auto-cfb8u)."
)


@dataclass(frozen=True)
class Violation:
    """A handler call site that passes a tainted org into Settings.

    ``path`` is whatever the caller passed (typically an absolute path
    on disk, or a placeholder like ``"<source>"`` for synthetic input).
    ``lineno`` is 1-based and points at the offending call.
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

    violations: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not _has_request_param(node):
            continue
        violations.extend(_scan_handler(node, path))
    return violations


def find_violations_in_repo(
    roots: Iterable[Path] | None = None,
) -> list[Violation]:
    """Scan all ``.py`` files under the given roots.

    ``roots`` defaults to ``[REPO_ROOT/"tools", REPO_ROOT/"agents"]``.
    Hidden directories (``.git``, ``.venv``, etc.) and the
    ``tools/graph/checks`` package itself are skipped — the check
    contains documentation strings that would otherwise trip its own
    string-pool inspection.
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
                continue
            try:
                source = py.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            violations.extend(find_violations_in_source(source, str(py)))
    return violations


# ── helpers ────────────────────────────────────────────────────────


def _has_request_param(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    args = func.args
    candidates: list[ast.arg] = []
    candidates.extend(args.posonlyargs)
    candidates.extend(args.args)
    candidates.extend(args.kwonlyargs)
    return any(a.arg == "request" for a in candidates)


def _scan_handler(
    func: ast.FunctionDef | ast.AsyncFunctionDef, path: str
) -> list[Violation]:
    """Walk handler statements in source order, tracking org-taint state."""
    tainted: set[str] = set()
    violations: list[Violation] = []
    for stmt in _iter_in_order(func):
        if isinstance(stmt, ast.Assign):
            _update_taint_from_assign(stmt, tainted)
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            _update_taint_from_ann_assign(stmt, tainted)
        for call_node in _iter_calls(stmt):
            v = _check_call(call_node, tainted, path)
            if v is not None:
                violations.append(v)
    return violations


def _iter_in_order(func: ast.FunctionDef | ast.AsyncFunctionDef):
    """Yield statements inside ``func`` in source order, descending into
    nested control flow so a later else/except can re-taint or untaint
    the same name correctly."""
    stack: list[ast.stmt] = list(func.body)
    while stack:
        stmt = stack.pop(0)
        yield stmt
        # Enqueue child statements (preserves overall doc order well
        # enough for the patterns this check targets — handlers are
        # mostly straight-line code with a few early returns).
        children: list[ast.stmt] = []
        for field, value in ast.iter_fields(stmt):
            if field in {"body", "orelse", "finalbody"}:
                if isinstance(value, list):
                    children.extend(s for s in value if isinstance(s, ast.stmt))
            elif field == "handlers" and isinstance(value, list):
                for h in value:
                    if isinstance(h, ast.ExceptHandler):
                        children.extend(
                            s for s in h.body if isinstance(s, ast.stmt)
                        )
        stack[:0] = children


def _iter_calls(stmt: ast.stmt):
    """Yield every ast.Call inside this statement's own expression slots
    — do *not* descend into nested statements (e.g. ``Try.body`` items),
    which ``_iter_in_order`` will yield separately. Without this guard a
    call inside a try/if/for body gets attributed to its enclosing
    statement *and* to itself, doubling violation counts."""

    def visit(node: ast.AST):
        # Skip nested statement scopes: the outer iterator will yield
        # them later. We still descend through expression nodes inside
        # the current statement (Call args, BoolOps, IfExps, etc.).
        if node is not stmt and isinstance(node, ast.stmt):
            return
        # Don't descend into nested function/class defs; if they qualify
        # as handlers, ``find_violations_in_source`` scans them in their
        # own pass.
        if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
        ) and node is not stmt:
            return
        if isinstance(node, ast.Call):
            yield node
        for child in ast.iter_child_nodes(node):
            yield from visit(child)

    yield from visit(stmt)


def _update_taint_from_assign(stmt: ast.Assign, tainted: set[str]) -> None:
    is_tainted = _value_is_tainted(stmt.value)
    for target in stmt.targets:
        if isinstance(target, ast.Name):
            _set_taint(target.id, is_tainted, tainted)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                if isinstance(elt, ast.Name):
                    _set_taint(elt.id, False, tainted)


def _update_taint_from_ann_assign(stmt: ast.AnnAssign, tainted: set[str]) -> None:
    if stmt.value is None or not isinstance(stmt.target, ast.Name):
        return
    is_tainted = _value_is_tainted(stmt.value)
    _set_taint(stmt.target.id, is_tainted, tainted)


def _set_taint(name: str, is_tainted: bool, tainted: set[str]) -> None:
    if is_tainted:
        tainted.add(name)
    else:
        tainted.discard(name)


def _value_is_tainted(value: ast.expr) -> bool:
    """A value is tainted iff it is exactly ``_caller_org(...)`` with no
    ``or CALLER_ORG`` fallback and no further composition."""
    return _is_caller_org_call(value)


def _is_caller_org_call(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == _CALLER_ORG_NAME
    )


def _is_caller_org_attr(node: ast.expr) -> bool:
    """True for ``settings_ops.CALLER_ORG`` / ``graph_ops.CALLER_ORG``,
    or a bare ``CALLER_ORG`` reference."""
    if isinstance(node, ast.Attribute) and node.attr == "CALLER_ORG":
        return (
            isinstance(node.value, ast.Name)
            and node.value.id in _SETTINGS_MODULE_NAMES
        )
    if isinstance(node, ast.Name) and node.id == "CALLER_ORG":
        return True
    return False


def _expression_has_caller_org_fallback(node: ast.expr) -> bool:
    """True for expressions that already pin a CALLER_ORG fallback —
    ``_caller_org(request) or graph_ops.CALLER_ORG``,
    ``org or graph_ops.CALLER_ORG`` (BoolOp), or
    ``graph_ops.CALLER_ORG if not org else org`` (IfExp)."""
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
        return any(_is_caller_org_attr(v) for v in node.values)
    if isinstance(node, ast.IfExp):
        return any(
            _is_caller_org_attr(part) for part in (node.body, node.orelse)
        )
    return False


def _is_settings_api_call(node: ast.Call) -> tuple[str, str] | None:
    """If ``node`` calls ``settings_ops.X`` or ``graph_ops.X`` where X
    is a public Settings API function, return ``(module_name, func)``.
    Otherwise ``None``."""
    func = node.func
    if not isinstance(func, ast.Attribute):
        return None
    if not isinstance(func.value, ast.Name):
        return None
    module = func.value.id
    if module not in _SETTINGS_MODULE_NAMES:
        return None
    if func.attr not in SETTINGS_API_FUNCS:
        return None
    return (module, func.attr)


def _get_org_kwarg(call: ast.Call) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == "org":
            return kw.value
    return None


def _check_call(
    call: ast.Call,
    tainted: set[str],
    path: str,
) -> Violation | None:
    target = _is_settings_api_call(call)
    if target is None:
        return None
    org_value = _get_org_kwarg(call)
    if org_value is None:
        return None
    if _expression_has_caller_org_fallback(org_value):
        return None
    if isinstance(org_value, ast.Name) and org_value.id in tainted:
        origin = f"_caller_org(request) (via local '{org_value.id}')"
    elif _is_caller_org_call(org_value):
        origin = "_caller_org(request)"
    else:
        return None
    module, func = target
    return Violation(
        path=path,
        lineno=call.lineno,
        reason=_VIOLATION_REASON.format(origin=origin, module=module, func=func),
    )
