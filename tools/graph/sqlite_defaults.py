"""The graph package's view of the per-connection SQLite defaults: the
defaults themselves live in :mod:`tools.network.sqlite_defaults` (the
registry's shipped tree opens stores too and carries no ``tools.graph``);
this module re-exports them for every graph, dashboard and vault caller and
adds the process-wide install, which needs :mod:`tools.graph.sqlite_open_diag`.
"""

from __future__ import annotations

from tools.network.sqlite_defaults import SYNCHRONOUS, apply  # noqa: F401 — re-exported

__all__ = ["SYNCHRONOUS", "apply", "install"]


def install() -> None:
    """Apply the defaults to every file-backed connection this process
    opens from now on. Idempotent."""
    from tools.graph import sqlite_open_diag

    sqlite_open_diag.install()
