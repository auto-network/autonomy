"""Which code opens SQLite connections, and which of them stay open.

auto-bkv3p: a busy dashboard's open file descriptors on orgs/autonomy.db
climb about ten a minute, and closing dead threads' pooled connections did
not stop it. Rather than infer the leaking site, this records it:
:func:`install` wraps ``sqlite3.connect`` for the whole process, and every
file-backed open is counted under its opener — a deduplicated stack of the
repository frames that called it. :func:`snapshot` reports, per database
file and opener, how many connections were opened and how many are still
open now. The site whose ``open_now`` grows with load is the leak.

A connection is "open now" while it is neither closed nor garbage
collected. The base ``sqlite3.Connection`` takes no weak reference, so a
call without a ``factory`` gets a trivial subclass that does; a caller's own
factory is subclassed the same way. Nothing else about the connection
changes.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import threading
import weakref
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parents[2]) + "/"
_STACK_DEPTH = 6
_THIS_FILE = "tools/graph/sqlite_open_diag.py"

_lock = threading.RLock()
_original_connect = None
#: (database file name, opener signature) -> connections opened
_opened: dict[tuple[str, str], int] = {}
#: id(connection) -> (weak reference, key)
_live: dict[int, tuple["weakref.ref", tuple[str, str]]] = {}
_factories: dict[type, type] = {}


def _traceable(factory: type) -> type:
    """A subclass of *factory* whose instances take weak references."""
    cls = _factories.get(factory)
    if cls is None:
        cls = factory if factory.__weakrefoffset__ else type(
            factory.__name__, (factory,), {"__slots__": ("__weakref__",)},
        )
        _factories[factory] = cls
    return cls


_repo_paths: dict[str, str | None] = {}


def _repo_path(filename: str) -> str | None:
    """*filename* relative to the repository, or None outside it. Code
    imported through a relative ``sys.path`` entry carries a relative
    filename, so it is made absolute first (and the answer cached)."""
    try:
        return _repo_paths[filename]
    except KeyError:
        pass
    if filename.startswith("<"):  # <string>, <frozen ...>
        _repo_paths[filename] = None
        return None
    absolute = os.path.abspath(filename)
    path = absolute[len(_REPO_ROOT):] if absolute.startswith(_REPO_ROOT) else None
    _repo_paths[filename] = path
    return path


def _opener() -> str:
    """The repository frames that led to this open, innermost first."""
    frames = []
    frame = sys._getframe(2)
    while frame is not None and len(frames) < _STACK_DEPTH:
        path = _repo_path(frame.f_code.co_filename)
        if path is not None and path != _THIS_FILE:
            frames.append(f"{path}:{frame.f_lineno} {frame.f_code.co_name}")
        frame = frame.f_back
    return " < ".join(frames) or "(outside the repository)"


def _database_name(database) -> str | None:
    text = str(database)
    if text in ("", ":memory:") or "mode=memory" in text:
        return None
    if text.startswith("file:"):
        text = text[5:].split("?", 1)[0]
    return Path(text).name


def _forget(conn_id: int) -> None:
    with _lock:
        _live.pop(conn_id, None)


def _connect(database, *args, **kwargs):
    name = _database_name(database)
    if name is None:
        return _original_connect(database, *args, **kwargs)
    if len(args) >= 5:  # factory passed positionally, after check_same_thread
        args = (*args[:4], _traceable(args[4]), *args[5:])
    else:
        kwargs["factory"] = _traceable(kwargs.get("factory") or sqlite3.Connection)
    conn = _original_connect(database, *args, **kwargs)
    key = (name, _opener())
    conn_id = id(conn)
    ref = weakref.ref(conn, lambda _r, conn_id=conn_id: _forget(conn_id))
    with _lock:
        _opened[key] = _opened.get(key, 0) + 1
        _live[conn_id] = (ref, key)
    return conn


def install() -> None:
    """Wrap ``sqlite3.connect`` for this process. Idempotent."""
    global _original_connect
    with _lock:
        if _original_connect is not None:
            return
        _original_connect = sqlite3.connect
        sqlite3.connect = _connect


def _is_open(conn) -> bool:
    try:
        conn.in_transaction
    except sqlite3.ProgrammingError:
        return False
    return True


def snapshot(database: str | None = None) -> list[dict]:
    """Per database file and opener: opened, and still open now. Sorted by
    ``open_now``, largest first. *database* filters by file name."""
    with _lock:
        opened = dict(_opened)
        live = list(_live.values())
    open_now: dict[tuple[str, str], int] = {}
    for ref, key in live:
        conn = ref()
        if conn is not None and _is_open(conn):
            open_now[key] = open_now.get(key, 0) + 1
    rows = [
        {"database": name, "opener": opener, "opened": count,
         "open_now": open_now.get((name, opener), 0)}
        for (name, opener), count in opened.items()
        if database is None or name == database
    ]
    rows.sort(key=lambda row: (-row["open_now"], -row["opened"]))
    return rows
