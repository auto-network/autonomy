"""Changing pull_direct must take effect without re-arming anything.

The scheduler calls the peer-address provider every iteration, but the
provider used to close over the pull_direct VALUE read at arm time. Live on
home 2026-09-10 that made an authorized change a no-op: the row was flipped to
True at 02:00:25Z and nothing ever dialled, because every armed process had
captured False seconds to minutes earlier. There was no failed dial and no
candidate error to find — the address list was simply empty and stayed empty.

A durable Setting that the operator changes and that silently does not apply
is worse than one that fails loudly, so this pins the re-read.
"""

from __future__ import annotations

import pytest

from tools.dashboard import fleet_enrollment_routes as fer


class _Cache:
    def __init__(self, **kw):
        pass

    def peers(self):
        return {"peer-1": ["ws://10.0.0.1:9410"]}


@pytest.fixture
def provider(monkeypatch):
    """The real provider, with the reachability cache and env peers stubbed."""
    from tools.network import fleet_direct_config, fleet_reachability

    state = {"pull_direct": False}
    monkeypatch.setattr(fleet_reachability, "ReachabilityCache", _Cache)
    monkeypatch.setattr(fer, "_fleet_env_peers", lambda: {})
    monkeypatch.setattr(
        fleet_direct_config, "load",
        lambda: type("_C", (), {"pull_direct": state["pull_direct"]})(),
    )
    credential = type("_Cred", (), {"machine_key": None,
                                    "reachability_cert": None})()
    peers = fer._reachability_peer_addresses(
        credential, "ab" * 32, pull_direct=state["pull_direct"],
    )
    return peers, state


def test_turning_it_on_reaches_the_scheduler_without_a_re_arm(provider):
    peers, state = provider
    assert peers() == {}, "armed with pull_direct False"
    state["pull_direct"] = True          # the operator flips the row
    assert peers() == {"peer-1": ["ws://10.0.0.1:9410"]}


def test_turning_it_off_also_applies_immediately(provider):
    peers, state = provider
    state["pull_direct"] = True
    assert peers() != {}
    state["pull_direct"] = False
    assert peers() == {}, "a machine told to stop dialling must stop"


def test_an_unreadable_row_keeps_the_armed_value(monkeypatch):
    """A Settings read that throws must not strand the tier in either
    direction — it falls back to what this process was armed with."""
    from tools.network import fleet_direct_config, fleet_reachability

    monkeypatch.setattr(fleet_reachability, "ReachabilityCache", _Cache)
    monkeypatch.setattr(fer, "_fleet_env_peers", lambda: {})

    def boom():
        raise OSError("settings unavailable")

    monkeypatch.setattr(fleet_direct_config, "load", boom)
    credential = type("_Cred", (), {"machine_key": None,
                                    "reachability_cert": None})()
    armed_on = fer._reachability_peer_addresses(
        credential, "ab" * 32, pull_direct=True,
    )
    armed_off = fer._reachability_peer_addresses(
        credential, "ab" * 32, pull_direct=False,
    )
    assert armed_on() == {"peer-1": ["ws://10.0.0.1:9410"]}
    assert armed_off() == {}
