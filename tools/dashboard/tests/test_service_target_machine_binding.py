"""A published host belongs to the machine its target row names.

ServiceTargetV1 is keyed by reservation UUID, is @home("organization") so it
replicates to every member, and carries a REQUIRED machine_id -- its docstring
says the row "freezes the local machine, Dashboard session, container
incarnation, and TCP port". The row replicates so every machine can SEE the
binding, not so every machine can act on it.

target_projection dropped machine_id, so every machine asked its connector to
serve every reservation in the org. The relay routes a hostname to exactly one
tunnel and refuses the rest with lease-held, and the condition never clears:
716 refusals against 16 successes for one org in an hour on registry-ash-1,
2026-09-10.

Invisible before that day because auto-clune.7 landed then -- until it, one
machine per fleet ran a connector, so only one machine could act on the row
whatever the projection said.
"""

from __future__ import annotations

import pytest

from tools.dashboard import service_publication, web_gateway_supervisor

MINE = "aa" * 32
THEIRS = "bb" * 32


def _payload(machine_id, session="auto-0101-000000"):
    return {
        "machine_id": machine_id,
        "session_id": session,
        "container_id": "c" * 64,
        "network": "autonomy_default",
        "port": 8790,
        "created_at": "2026-09-10T00:00:00.000Z",
        "updated_at": "2026-09-10T00:00:00.000Z",
    }


def test_the_projection_carries_the_machine_that_owns_the_row():
    """Dropping it is what made every row look like it belonged to everyone."""
    row = service_publication.target_projection("res-1", _payload(MINE))
    assert row["machine_id"] == MINE


def test_only_reservations_naming_this_machine_are_served(monkeypatch):
    monkeypatch.setattr(service_publication, "_read_local_machine_id",
                        lambda: MINE)
    monkeypatch.setattr(
        service_publication, "list_service_targets",
        lambda org: [
            service_publication.target_projection("mine-1", _payload(MINE)),
            service_publication.target_projection("theirs-1", _payload(THEIRS)),
            service_publication.target_projection("theirs-2", _payload(THEIRS)),
        ])

    assert web_gateway_supervisor._local_reservation_ids("autonomy") == {"mine-1"}


def test_an_unreadable_identity_serves_nothing_rather_than_everything(monkeypatch):
    """A machine that cannot say who it is must not claim to own anything.
    Failing open here would restore the exact storm this closes."""
    monkeypatch.setattr(service_publication, "_read_local_machine_id",
                        lambda: None)
    monkeypatch.setattr(
        service_publication, "list_service_targets",
        lambda org: [
            service_publication.target_projection("mine-1", _payload(MINE)),
        ])

    assert web_gateway_supervisor._local_reservation_ids("autonomy") == set()


def test_a_migrated_target_stops_being_ours(monkeypatch):
    """When a row is rebound to another machine, this machine stops desiring
    the host on the next read -- so a migration hands over instead of two
    machines contending. There is no race to lose and nothing to back off
    from: lease-held under a correct supervisor means two machines believe
    they own one reservation, which is a fault and should stay loud."""
    monkeypatch.setattr(service_publication, "_read_local_machine_id",
                        lambda: MINE)
    rows = [service_publication.target_projection("res-1", _payload(MINE))]
    monkeypatch.setattr(service_publication, "list_service_targets",
                        lambda org: rows)
    assert web_gateway_supervisor._local_reservation_ids("autonomy") == {"res-1"}

    rows[0] = service_publication.target_projection("res-1", _payload(THEIRS))
    assert web_gateway_supervisor._local_reservation_ids("autonomy") == set()
