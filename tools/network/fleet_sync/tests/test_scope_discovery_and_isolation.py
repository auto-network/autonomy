"""One scope must not starve the others, and a stray file is not a scope.

Live fault, 2026-09-07T20:28 to 2026-09-08T21:20: a stray `None.db` in
data/orgs/ became a scope named 'None', sorted ahead of every real
organization, failed its pull, and `_sync_peer` returned on that failure --
so anchore, autonomy, blindhash and dynbench were never requested for 24
hours. personal was iterated first and kept working, so every outcome-based
check read green.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from tools.network.fleet_sync_scheduler import discover_org_sync_scopes


def test_a_stray_file_is_not_a_scope(tmp_path, monkeypatch):
    orgs = tmp_path / "orgs"
    orgs.mkdir()
    for name in (
        "autonomy", "anchore", "enterprise-ng",      # real slugs
        "None", "personal", "machine",               # never scopes
        "2d4b90cb-1e89-452b-82cb-68ca44fd8e52",      # an org uuid, not a slug
        "Weird_Name", "9leading",                    # not slug-shaped
    ):
        (orgs / f"{name}.db").write_bytes(b"")
    monkeypatch.setattr(
        "tools.graph.db._org_db_path", lambda slug, root=None: tmp_path / "personal.db"
    )
    assert set(discover_org_sync_scopes()) == {"autonomy", "anchore", "enterprise-ng"}


def test_one_failing_scope_does_not_starve_the_rest():
    """The regression itself: a scope that fails must not skip those after it."""
    from tools.network import fleet_sync_scheduler as fss

    attempted: list[str] = []

    class _Scheduler:
        _scope_paths = staticmethod(lambda: {
            "personal": Path("p"), "None": Path("n"),
            "anchore": Path("a"), "autonomy": Path("b"),
        })
        config = None

        async def _sync_scope(self, machine_pub, addresses, scope, *, org_channel=None):
            attempted.append(scope)
            # The stray scope fails while the PEER is reachable.
            return "ok" if scope != "None" else "scope_failed"

    sched = _Scheduler()
    asyncio.run(fss.FleetSyncScheduler._sync_peer(sched, "peer", ()))
    assert attempted == ["personal", "None", "anchore", "autonomy"], attempted


def test_an_unreachable_peer_still_stops_the_round():
    """The other half: if the DIAL fails there is no point trying the rest,
    and hammering every scope with the same failed dial is what the original
    early return was protecting against."""
    from tools.network import fleet_sync_scheduler as fss

    attempted: list[str] = []

    class _Scheduler:
        _scope_paths = staticmethod(lambda: {
            "personal": Path("p"), "anchore": Path("a"), "autonomy": Path("b"),
        })
        config = None

        async def _sync_scope(self, machine_pub, addresses, scope, *, org_channel=None):
            attempted.append(scope)
            return "peer_unreachable"

    asyncio.run(fss.FleetSyncScheduler._sync_peer(_Scheduler(), "peer", ()))
    assert attempted == ["personal"], attempted
