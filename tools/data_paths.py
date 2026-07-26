"""Shared safety controls for resolving operator data directories.

Production keeps its historical repository-local defaults. Tests and other
isolated callers can set ``AUTONOMY_REFUSE_REAL_DATA_FALLBACK=1`` to require an
explicit root; an omitted isolation variable then fails loudly at resolution
time instead of touching the operator's live ``data/`` tree.
"""

from __future__ import annotations

import os
from pathlib import Path


REFUSE_REAL_DATA_FALLBACK_ENV = "AUTONOMY_REFUSE_REAL_DATA_FALLBACK"
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


class RealDataFallbackRefused(RuntimeError):
    """An isolated caller attempted to use a repository-local data default."""


def refuse_real_data_fallback_enabled() -> bool:
    """Return whether repository-local fallback paths must be refused."""
    value = os.environ.get(REFUSE_REAL_DATA_FALLBACK_ENV, "")
    return value.strip().lower() in _TRUE_VALUES


def resolve_orgs_root(
    root: Path | str | None,
    *,
    default: Path,
) -> Path:
    """Resolve an organization DB root without silently escaping isolation."""
    if root is not None:
        return Path(root)
    env = os.environ.get("AUTONOMY_ORGS_DIR")
    if env:
        return Path(env)
    if refuse_real_data_fallback_enabled():
        raise RealDataFallbackRefused(
            "refusing repository data/orgs fallback: set AUTONOMY_ORGS_DIR "
            "or pass an explicit org root"
        )
    return default
