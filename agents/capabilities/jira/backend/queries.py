"""Named-query resolution for the ``issue_tracker`` capability.

Named queries are *workspace data*, not capability code: the
``workspace_overrides`` object on a workspace's
``autonomy.workspace.capability.enable`` Setting carries

* ``named_queries`` — a list of ``{name, summary, query}`` rows, the
  ``query`` written in the provider's own query language (JQL for the
  autonomy/jira impl) with ``{placeholder}`` slots, and
* ``query_defaults`` — placeholder values shared by every query in the
  workspace (e.g. ``{"project": "ENTERPRISE"}``).

This module is pure: it never reads the graph or the network. The broker
resolves the enable Setting for the calling session's workspace and hands
the overrides dict here; the returned query string goes to the provider's
search op.

A query's caller-supplied params are *derived*: the placeholders in its
``query`` text minus the keys of ``query_defaults``. There is no separate
``params`` field to drift out of sync with the query.

Substituted values are held to a conservative character allowlist so a
param can't smuggle extra clauses into the query. This is hygiene, not a
trust boundary — the raw search op accepts arbitrary provider queries and
is equally read-only.
"""

from __future__ import annotations

import re
from typing import Any

_PLACEHOLDER_RE = re.compile(r"\{([a-z][a-z0-9_]*)\}")
# Covers project keys, semver-ish versions ("6.1.0"), status/version names
# with spaces ("Enterprise 6.1.0"), and account-ish values — but no quotes,
# parens, or operators, so a value can't terminate a string literal or
# splice clauses.
_VALUE_RE = re.compile(r"^[A-Za-z0-9._@ -]{1,100}$")


class QueryError(Exception):
    """A named query could not be listed or resolved. Messages are
    agent-facing and actionable (they name the missing/invalid param)."""


def _query_rows(overrides: dict | None) -> list[dict]:
    rows = (overrides or {}).get("named_queries")
    if not isinstance(rows, list):
        return []
    return [r for r in rows
            if isinstance(r, dict)
            and isinstance(r.get("name"), str) and r["name"]
            and isinstance(r.get("query"), str) and r["query"]]


def _defaults(overrides: dict | None) -> dict[str, str]:
    d = (overrides or {}).get("query_defaults")
    if not isinstance(d, dict):
        return {}
    return {str(k): str(v) for k, v in d.items()}


def _caller_params(row: dict, defaults: dict[str, str]) -> list[str]:
    """Placeholders the caller must supply (not covered by defaults),
    in first-appearance order."""
    seen: list[str] = []
    for name in _PLACEHOLDER_RE.findall(row["query"]):
        if name not in defaults and name not in seen:
            seen.append(name)
    return seen


def list_queries(overrides: dict | None) -> list[dict[str, Any]]:
    """The workspace's named queries, shaped for discovery:
    ``[{name, summary, params}, …]`` where ``params`` are the
    caller-supplied placeholder names."""
    defaults = _defaults(overrides)
    return [{
        "name": row["name"],
        "summary": str(row.get("summary") or ""),
        "params": _caller_params(row, defaults),
    } for row in _query_rows(overrides)]


def resolve_query(overrides: dict | None, name: str,
                  params: dict[str, str]) -> str:
    """Return the provider query for named query *name* with every
    ``{placeholder}`` substituted from *params* + ``query_defaults``
    (params win). Raises :class:`QueryError` on an unknown query name,
    a missing or unused param, or a value outside the allowlist."""
    rows = {row["name"]: row for row in _query_rows(overrides)}
    row = rows.get(name)
    if row is None:
        known = ", ".join(sorted(rows)) or "none defined for this workspace"
        raise QueryError(f"unknown named query {name!r} (known: {known})")

    placeholders = set(_PLACEHOLDER_RE.findall(row["query"]))
    unused = sorted(set(params) - placeholders)
    if unused:
        raise QueryError(
            f"query {name!r} takes no param(s): {', '.join(unused)}")
    values = {**_defaults(overrides), **{k: str(v) for k, v in params.items()}}
    missing = [p for p in _caller_params(row, {}) if p not in values]
    if missing:
        raise QueryError(
            f"query {name!r} requires: "
            + ", ".join(f"{p}=<value>" for p in missing))
    for key in placeholders:
        if not _VALUE_RE.match(values[key]):
            raise QueryError(
                f"invalid value for {key!r}: only letters, digits, spaces, "
                "and . _ @ - are allowed (max 100 chars)")

    def _sub(match: re.Match) -> str:
        return values[match.group(1)]

    return _PLACEHOLDER_RE.sub(_sub, row["query"])
