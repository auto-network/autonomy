"""Dashboard read helpers for ``dashboard.feature_flags#1``.

Thin facade over :mod:`tools.graph.settings_ops` for boolean feature
flags. Missing rows always read as ``False`` — consumers gate behavior
on a flag by calling :func:`is_enabled` and treating absent rows as
disabled.

No caching layer in v1: reads go through the existing
:mod:`settings_ops` primitive each call. Flag reads are infrequent
(per-render in the dashboard, per-session-start in WS handlers) and the
underlying Settings substrate is fast (SQLite). If a measured hot path
emerges, add :func:`functools.lru_cache` with explicit ``cache_clear()``
exposed for tests and writes — as its own slice.

Spec: graph://40dd9d7a-23a.
"""

from __future__ import annotations

from typing import Any

from tools.graph import settings_ops
from tools.graph.schemas.feature_flags import FEATURE_FLAGS_SET_ID


def is_enabled(
    name: str,
    *,
    org: "str | None | settings_ops._CallerOrgSentinel" = settings_ops.CALLER_ORG,
) -> bool:
    """Return True if the flag named *name* is enabled.

    Absent rows return ``False``. Malformed payloads (missing or
    non-bool ``enabled``) return ``False``; this helper never raises on
    a read-shaped error so downstream consumers can gate freely.

    ``org`` defaults to :data:`settings_ops.CALLER_ORG` (env-cascade
    resolution). Tests pass an explicit slug.
    """
    members = settings_ops.read_set(
        FEATURE_FLAGS_SET_ID,
        org=org,
        peers=[],
    )
    for m in members.members:
        if m.key == name:
            payload = m.payload or {}
            value = payload.get("enabled")
            return value is True
    return False


def all_flags(
    *,
    org: "str | None | settings_ops._CallerOrgSentinel" = settings_ops.CALLER_ORG,
) -> dict[str, dict[str, Any]]:
    """Return a snapshot ``{flag_name: payload}`` of all known flag rows.

    Useful for the Settings UI inspector or test introspection. Order
    is not guaranteed. Empty when the set has no members.
    """
    members = settings_ops.read_set(
        FEATURE_FLAGS_SET_ID,
        org=org,
        peers=[],
    )
    return {m.key: dict(m.payload or {}) for m in members.members}
