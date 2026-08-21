"""SQLite connection boundary for production-authored fleet mutations.

The synchronization catalog's triggers are deliberately fail-closed, but the
three production stores that share ``personal.db`` historically own their own
connections and transaction scopes.  This connection subclass supplies one
small lifecycle seam: a catalog hook may enter an authored context before a
replicated statement and finalize or discard it with the exact SQLite
commit/rollback that owns the application row.

No SQL is interpreted here.  The attached catalog decides which statements
need authorship; statements it does not recognize still meet fail-closed
triggers if they mutate a replicated table.

The same connection boundary is also the only process-wide checkpoint swap
gate. A quiescence token prevents new production connections and is issued
only after every registered handle for that database has closed.
"""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import threading
from typing import Protocol
import weakref


class FleetSyncQuiescenceError(sqlite3.OperationalError):
    """A personal database cannot enter or has violated a checkpoint handoff."""


_QUIESCENCE_LOCK = threading.RLock()
_OPEN_CONNECTIONS: dict[str, weakref.WeakSet] = {}
_QUIESCED: dict[str, object] = {}


def _database_key(database: object) -> str | None:
    try:
        raw = os.fspath(database)
    except TypeError:
        return None
    if isinstance(raw, bytes):
        raw = os.fsdecode(raw)
    if (
        raw == ""
        or raw == ":memory:"
        or raw.startswith("file::memory:")
        or (raw.startswith("file:") and "mode=memory" in raw)
    ):
        return None
    if raw.startswith("file:"):
        raw = raw[5:].split("?", 1)[0]
    return str(Path(raw).resolve())


class DatabaseQuiescence:
    """Exclusive, process-local authority to replace one SQLite database.

    Acquisition first prevents new production writer connections, then
    refuses unless every existing production connection has closed. The
    token stays live across staging publication so no writer can retain the
    old inode or open the replacement before validation completes.
    """

    def __init__(self, path: str, nonce: object):
        self.path = path
        self._nonce = nonce
        self._active = True

    def release(self) -> None:
        if not self._active:
            return
        with _QUIESCENCE_LOCK:
            if _QUIESCED.get(self.path) is not self._nonce:
                raise FleetSyncQuiescenceError(
                    "database quiescence authority is no longer current"
                )
            del _QUIESCED[self.path]
            self._active = False

    def __enter__(self) -> "DatabaseQuiescence":
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


def acquire_database_quiescence(path: str | Path) -> DatabaseQuiescence:
    """Gate new production connections and require all existing ones closed."""
    key = _database_key(path)
    if key is None:
        raise FleetSyncQuiescenceError("in-memory databases cannot be replaced")
    nonce = object()
    with _QUIESCENCE_LOCK:
        if key in _QUIESCED:
            raise FleetSyncQuiescenceError("database is already quiesced")
        _QUIESCED[key] = nonce
        open_count = len(_OPEN_CONNECTIONS.get(key, ()))
        if open_count:
            del _QUIESCED[key]
            raise FleetSyncQuiescenceError(
                f"database still has {open_count} live production connection(s)"
            )
    return DatabaseQuiescence(key, nonce)


def require_database_quiescence(
    token: DatabaseQuiescence, path: str | Path
) -> None:
    key = _database_key(path)
    with _QUIESCENCE_LOCK:
        if (
            key is None
            or token.path != key
            or not token._active
            or _QUIESCED.get(key) is not token._nonce
        ):
            raise FleetSyncQuiescenceError(
                "a current quiescence token for the target database is required"
            )


class AuthoredTransactionHook(Protocol):
    def before_statement(self, sql: object) -> bool:
        """Enter authorship when needed; return whether an error owns rollback."""

    def before_commit(self) -> None:
        """Finalize authored metadata inside the pending SQLite transaction."""

    def after_transaction(self) -> None:
        """Forget in-memory transaction state after commit or rollback."""


class FleetSyncCursor(sqlite3.Cursor):
    """Cursor variant that preserves the same authored boundary as connection DML."""

    def execute(self, sql, parameters=(), /):
        connection = self.connection
        hook = connection._hook()
        rollback_on_error = hook.before_statement(sql) if hook is not None else False
        try:
            return super().execute(sql, parameters)
        except Exception:
            if rollback_on_error:
                sqlite3.Connection.rollback(connection)
                hook.after_transaction()
            raise

    def executemany(self, sql, seq_of_parameters, /):
        connection = self.connection
        hook = connection._hook()
        rollback_on_error = hook.before_statement(sql) if hook is not None else False
        try:
            return super().executemany(sql, seq_of_parameters)
        except Exception:
            if rollback_on_error:
                sqlite3.Connection.rollback(connection)
                hook.after_transaction()
            raise


class FleetSyncConnection(sqlite3.Connection):
    """A normal SQLite connection with an optional authored-write hook."""

    def __init__(self, database, *args, **kwargs):
        key = _database_key(database)
        with _QUIESCENCE_LOCK:
            if key is not None and key in _QUIESCED:
                raise FleetSyncQuiescenceError(
                    "database is quiesced for checkpoint handoff"
                )
            super().__init__(database, *args, **kwargs)
            self._fleet_sync_path_key = key
            self._fleet_sync_registered = key is not None
            if key is not None:
                _OPEN_CONNECTIONS.setdefault(key, weakref.WeakSet()).add(self)

    def install_fleet_sync_hook(self, hook: AuthoredTransactionHook) -> None:
        current = getattr(self, "_fleet_sync_hook", None)
        if current is not None and current is not hook:
            raise sqlite3.IntegrityError(
                "fleet-sync connection already has an authored-write hook"
            )
        self._fleet_sync_hook = hook

    def _hook(self) -> AuthoredTransactionHook | None:
        return getattr(self, "_fleet_sync_hook", None)

    def cursor(self, factory=FleetSyncCursor):
        return super().cursor(factory)

    def execute(self, sql, parameters=(), /):
        hook = self._hook()
        rollback_on_error = hook.before_statement(sql) if hook is not None else False
        try:
            return super().execute(sql, parameters)
        except Exception:
            if rollback_on_error:
                super().rollback()
                hook.after_transaction()
            raise

    def executemany(self, sql, seq_of_parameters, /):
        hook = self._hook()
        rollback_on_error = hook.before_statement(sql) if hook is not None else False
        try:
            return super().executemany(sql, seq_of_parameters)
        except Exception:
            if rollback_on_error:
                super().rollback()
                hook.after_transaction()
            raise

    def executescript(self, sql_script, /):
        if self._hook() is not None:
            # sqlite3_exec commits a pending transaction before parsing the
            # script, bypassing the authored commit hook. Schema upgrades on
            # an activated store therefore need an explicit coordinated path.
            raise sqlite3.IntegrityError(
                "fleet-sync activated connection refuses executescript"
            )
        return super().executescript(sql_script)

    def commit(self) -> None:
        hook = self._hook()
        try:
            if hook is not None:
                hook.before_commit()
            super().commit()
        except Exception:
            super().rollback()
            if hook is not None:
                hook.after_transaction()
            raise
        if hook is not None:
            hook.after_transaction()

    def rollback(self) -> None:
        hook = self._hook()
        try:
            super().rollback()
        finally:
            if hook is not None:
                hook.after_transaction()

    def __enter__(self) -> "FleetSyncConnection":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False

    def close(self) -> None:
        hook = self._hook()
        try:
            with _QUIESCENCE_LOCK:
                super().close()
                if getattr(self, "_fleet_sync_registered", False):
                    key = self._fleet_sync_path_key
                    connections = _OPEN_CONNECTIONS.get(key)
                    if connections is not None:
                        connections.discard(self)
                        if not connections:
                            _OPEN_CONNECTIONS.pop(key, None)
                    self._fleet_sync_registered = False
        finally:
            if hook is not None:
                hook.after_transaction()
