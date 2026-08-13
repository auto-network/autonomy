"""Dashboard read helpers for ``dashboard.feature_flags#1``.

Thin facade over :mod:`tools.graph.settings_ops` for boolean feature
flags. Missing rows always read as ``False`` — consumers gate behavior
on a flag by calling :func:`is_enabled` and treating absent rows as
disabled.

Snapshots are cached once per resolved org and invalidated by the dashboard's
post-commit ``setting.changed`` hook. This keeps flag checks safe in hot paths
(notably voice audio frames) without sacrificing live flag changes.

Spec: graph://40dd9d7a-23a.
"""

from __future__ import annotations

import threading
from typing import Any

from tools.graph import settings_ops
from tools.graph.schemas.feature_flags import FEATURE_FLAGS_SET_ID

_cache_lock = threading.RLock()
_flags_by_org: dict[str | None, dict[str, dict[str, Any]]] = {}

# Feature flags are OPERATOR-LOCAL config (rubric graph://4d88c2ad-625): their
# correct value depends on this operator's own machine/instance, so they live in
# personal.db. Reads must PIN to personal — not inherit the process's ambient
# GRAPH_ORG (the dashboard runs GRAPH_ORG=autonomy, which read them from the wrong
# org and silently disabled voice dictation). Mirrors _credentials_org().
_FLAGS_ORG = "personal"


def _resolved_org(
    org: "str | None | settings_ops._CallerOrgSentinel",
) -> str | None:
    # Cache by the actual request/env-cascade result, not by the shared
    # CALLER_ORG sentinel, or two request orgs could share one snapshot.
    return settings_ops._resolve_org_arg(org)


def invalidate_cache(*, org: str | None = None, all_orgs: bool = False) -> None:
    """Drop one org's flag snapshot, or every snapshot at lifecycle reset."""
    with _cache_lock:
        if all_orgs:
            _flags_by_org.clear()
        else:
            _flags_by_org.pop(org, None)


def _flags_snapshot(
    org: "str | None | settings_ops._CallerOrgSentinel",
) -> dict[str, dict[str, Any]]:
    resolved_org = _resolved_org(org)
    with _cache_lock:
        snapshot = _flags_by_org.get(resolved_org)
        if snapshot is None:
            members = settings_ops.read_set(
                FEATURE_FLAGS_SET_ID, org=resolved_org, peers=[],
            )
            snapshot = {m.key: dict(m.payload or {}) for m in members.members}
            _flags_by_org[resolved_org] = snapshot
        return snapshot


def is_enabled(
    name: str,
    *,
    org: "str | None | settings_ops._CallerOrgSentinel" = _FLAGS_ORG,
    default: bool = False,
) -> bool:
    """Return True if the flag named *name* is enabled.

    Absent rows return ``default`` (``False`` unless the caller opts in —
    e.g. a flag W4 wants unflagged-on by default, like
    ``ingest.eager_sources``, passes ``default=True`` so a fresh
    deployment with no seeded Settings row still gets the behavior; an
    explicit ``{"enabled": false}`` row still disables it regardless of
    ``default``). Malformed payloads (missing or non-bool ``enabled``)
    return ``False`` — an existing-but-broken row never falls through to
    ``default``, only a genuinely absent row does. This helper never
    raises on a read-shaped error so downstream consumers can gate
    freely.

    ``org`` defaults to ``_FLAGS_ORG`` (``personal``) — feature flags are
    operator-local; reads pin to personal, not the ambient GRAPH_ORG. Tests
    (and any genuinely org-scoped flag) pass an explicit slug.
    """
    payload = _flags_snapshot(org).get(name)
    if payload is None:
        return default
    return payload.get("enabled") is True


def all_flags(
    *,
    org: "str | None | settings_ops._CallerOrgSentinel" = _FLAGS_ORG,
) -> dict[str, dict[str, Any]]:
    """Return a snapshot ``{flag_name: payload}`` of all known flag rows.

    Useful for the Settings UI inspector or test introspection. Order
    is not guaranteed. Empty when the set has no members.
    """
    return {name: dict(payload) for name, payload in _flags_snapshot(org).items()}
