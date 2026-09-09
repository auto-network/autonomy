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

"""

from __future__ import annotations

import sqlite3
from typing import Protocol


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
            super().close()
        finally:
            if hook is not None:
                hook.after_transaction()
