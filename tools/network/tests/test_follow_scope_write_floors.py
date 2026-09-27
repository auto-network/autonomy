"""A followed mirror has no write floor to seal.

The mirror is filled only by its org:follow pull; this machine writes
nothing into it and holds no org channel for it, so the write-floor pass
skips it instead of warning "persona write floor NOT sealed" every round
(the line auto-ffhwt's adoption was meant to end; fresh follower node,
compose simulation 2026-09-27).
"""

from __future__ import annotations

from types import SimpleNamespace

from tools.network.fleet_sync_scheduler import FleetSyncScheduler


class _Store:
    def __init__(self, name):
        self.name = name
        self.machine_floors = 0
        self.persona_floors = 0

    def seal_machine_write_floor(self, signer, now_ns, *, cert=None):
        self.machine_floors += 1
        return {"scope": self.name}

    def seal_persona_write_floor(self, **kwargs):
        self.persona_floors += 1
        return {"scope": self.name}


def test_followed_scopes_are_skipped_by_the_write_floor_pass(caplog):
    scheduler = object.__new__(FleetSyncScheduler)
    scheduler.config = SimpleNamespace(machine_key=object(), delegation_cert=None)
    stores = {name: _Store(name) for name in ("personal", "member", "autonomy")}
    scheduler._scope_paths = lambda: {name: None for name in stores}
    scheduler._store_for = lambda scope: stores[scope]
    scheduler._followed_scopes = lambda: {"autonomy"}
    scheduler._org_channels = lambda: {}
    scheduler._org_channel_absence = lambda scope: "no channel"

    with caplog.at_level("WARNING"):
        scheduler._seal_write_floors(set())

    assert stores["personal"].machine_floors == 1
    assert stores["member"].machine_floors == 1
    assert stores["autonomy"].machine_floors == 0
    assert stores["autonomy"].persona_floors == 0
    warned = [r.getMessage() for r in caplog.records if "NOT sealed" in r.getMessage()]
    assert warned and all("'member'" in line for line in warned)
    assert not any("'autonomy'" in line for line in warned)
