"""auto-azzvp: an org's ledger events reach another machine by replication,
not by a hand-copied table.

The design of record is graph://53b5bb04-bc0: ledger_events is an
append-only Setting homed in the organization, keyed by the event id, and
every other ledger table is a local index rebuilt from it. The transport
existed but never ran, because publishing was triggered only by a
successful pull -- so the machine holding the events was the one machine
that never published them.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path

from tools.graph.db import GraphDB
from tools.network import fleet_sync_scheduler as fss
from tools.network.fleet_sync.tests.test_org_channel_routing import (
    Member, _wait,
)
from tools.network.idkit import KeyPair
from tools.network.ledger.settings_bridge import SET_ID
from tools.network.ledger.store import LedgerStore


def _found(path: Path, *, now: int | None = None) -> tuple[str, int]:
    """Found an org ledger in the store at *path*; return (genesis, events)."""
    from tools.network.ledger.found import found_org_ledger

    with LedgerStore(path) as store:
        founded = found_org_ledger(
            store, org_id="11111111-1111-4111-8111-111111111111",
            org_root=KeyPair.generate(),
            personal_root_seed=b"\x11" * 32,
            now=int(time.time() if now is None else now),
        )
        return founded.genesis_id, len(list(store.events()))


def _rows(path: Path) -> set[str]:
    """Keys of the replicated ledger-event rows in the store at *path*."""
    if not Path(path).exists():
        return set()
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        try:
            return {
                str(r[0]) for r in conn.execute(
                    'SELECT "key" FROM settings WHERE set_id=?', (SET_ID,),
                )
            }
        except sqlite3.Error:
            return set()


def _drop_rows(path: Path) -> int:
    """Delete the replicated ledger-event rows, leaving the ledger intact.

    This is the state every ledger founded before the transport existed is
    in, and the state any ledger reaches when the inline publish hook fails
    (it is best-effort by contract, so a failure is swallowed). Written
    through GraphDB so the store's capture triggers see the deletion as any
    real writer would.
    """
    db = GraphDB(path)
    try:
        cursor = db.conn.execute("DELETE FROM settings WHERE set_id=?", (SET_ID,))
        db.conn.commit()
        return cursor.rowcount
    finally:
        db.close()


def _ledger_events(path: Path) -> set[str]:
    if not Path(path).exists():
        return set()
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        try:
            return {str(r[0]) for r in conn.execute("SELECT event_id FROM ledger_events")}
        except sqlite3.Error:
            return set()


def test_a_serving_origin_publishes_its_own_ledger_events(tmp_path: Path, monkeypatch) -> None:
    """The machine that founded the ledger publishes without pulling anything.

    This is the regression: before auto-azzvp the only publish trigger for
    pre-existing events was the post-pull hook, so an origin with no peer
    to pull from never published and the set stayed empty exactly where
    the events were.
    """
    pa = KeyPair.generate()
    a = Member(tmp_path, "a", pa, [pa.public_hex])
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(a.orgs_dir))
    genesis, event_count = _found(a.alpha)
    assert event_count >= 4 and genesis
    # Founding publishes inline, which covers events created after the
    # transport existed. Drop those rows to reproduce the state this bead
    # is about: a ledger whose events have no published rows, which is
    # every ledger founded before the transport and any whose inline
    # publish failed. Backfilling that was reconcile's job, and reconcile
    # only ran after a successful pull.
    assert _drop_rows(a.alpha) == event_count
    assert _rows(a.alpha) == set()

    async def run() -> None:
        # No peers at all: this machine can never complete a pull.
        scheduler = a.scheduler()
        await scheduler.start()
        try:
            await _wait(
                lambda: _rows(a.alpha) == _ledger_events(a.alpha),
                timeout=30.0, label="the origin publishes its own events",
            )
        finally:
            await scheduler.stop()

    asyncio.run(run())
    published = _rows(a.alpha)
    assert genesis in published
    assert len(published) == event_count


def test_publishing_is_rate_limited_and_idempotent(tmp_path: Path, monkeypatch) -> None:
    pa = KeyPair.generate()
    a = Member(tmp_path, "a", pa, [pa.public_hex])
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(a.orgs_dir))
    _found(a.alpha)
    _drop_rows(a.alpha)
    calls: list[str] = []
    real = fss.FleetSyncScheduler._reconcile_ledger

    async def counting(self, scope, *, force=False):
        calls.append(scope)
        return await real(self, scope, force=force)

    monkeypatch.setattr(fss.FleetSyncScheduler, "_reconcile_ledger", counting)

    async def run() -> None:
        scheduler = a.scheduler(poll=0.05)
        await scheduler.start()
        try:
            await _wait(lambda: _rows(a.alpha), timeout=30.0, label="published")
            before = len(_rows(a.alpha))
            await asyncio.sleep(1.0)   # many rounds inside the rate limit
            assert len(_rows(a.alpha)) == before, "republished rows"
        finally:
            await scheduler.stop()

    asyncio.run(run())
    # Many rounds attempted it; the timer kept the actual work to the
    # forced startup pass per scope.
    assert calls.count("alpha") > 2


def test_an_event_appended_later_is_published_without_any_pull(tmp_path: Path, monkeypatch) -> None:
    """Events appended while the scheduler runs publish inline; the periodic
    pass is the backstop for anything the inline hook missed."""
    pa = KeyPair.generate()
    a = Member(tmp_path, "a", pa, [pa.public_hex])
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(a.orgs_dir))
    _found(a.alpha)
    _drop_rows(a.alpha)

    async def run() -> None:
        scheduler = a.scheduler()
        await scheduler.start()
        try:
            await _wait(
                lambda: _rows(a.alpha) == _ledger_events(a.alpha),
                timeout=30.0, label="initial publish",
            )
            # A later authority event: a second role definition.
            from tools.network.ledger.events import make_event
            from tools.network.ledger.hlc import HLC

            with LedgerStore(a.alpha) as store:
                root = KeyPair.generate()
                # Signed by a key the fold will not accept is fine here:
                # publication is about transport, not authority.
                heads = list(store.ledger.heads())
                event = make_event(
                    root,
                    {"type": "role.define", "name": "reader",
                     "scope_set": ["read"], "claim_requires": "self", "version": 1},
                    heads, HLC(int(time.time()), 9),
                )
                store.append(event)
                new_id = event.event_id
            await _wait(
                lambda: new_id in _rows(a.alpha),
                timeout=30.0, label="the later event is published",
            )
        finally:
            await scheduler.stop()

    asyncio.run(run())
