"""Tunnel eligibility narrows to designation ONLY (auto-clune.7 patch A).

`fleet_tunnel_server.state()` is the fleet's singular-ownership election, and
`allowed` is consumed by things that have nothing to do with tunnels: claude and
codex credential refresh, usage-row maintenance, enrollment targeting. Its own
comment in `claude_credentials_refresh.py` says so — "so ONLY ONE Fleet machine
ever refreshes this credential". Widening `allowed` to enable tunnels on every
machine would let every machine refresh a single-use credential concurrently.

So this patch adds a narrower predicate and leaves `state()` alone. The tests
that matter are the two that would catch the change being wrong in each
direction: the election must still be singular (not weakened), and every safety
reason must still block serving (not over-permitted).
"""

from __future__ import annotations

import pytest

from tools.network import fleet_tunnel_server as fts


class _State:
    def __init__(self, allowed, reason):
        self.allowed = allowed
        self.reason = reason
        self.managed = True


def _state(monkeypatch, allowed, reason):
    monkeypatch.setattr(fts, "state", lambda: _State(allowed, reason))


def test_the_election_is_narrowed_not_weakened(monkeypatch):
    """THE ONE THAT MATTERS. On a non-designated machine, tunnel serving becomes
    permitted while `state().allowed` stays False — so credential refresh and
    usage maintenance, which gate on `allowed`, still run on exactly one
    machine. If this patch had widened `allowed` instead, this assertion is what
    fails."""
    _state(monkeypatch, False, "not-designated")

    assert fts.state().allowed is False
    assert fts.tunnel_serving_permitted()[0] is True


@pytest.mark.parametrize("reason", [
    "fleet-member-provisioning",
    "personal-root-missing",
    "roster-unreadable",
    "roster-empty",
    "roster-invalid",
    "assignment-inactive",
    "machine-identity-missing",
    "machine-not-rostered",
    "assignment-unreadable",
    "assignment-invalid",
])
def test_every_safety_reason_still_blocks_serving(monkeypatch, reason):
    """Only designation is relaxed. A mid-join machine in particular must still
    refuse: `state()` fails closed there deliberately so a joining machine never
    serves as a second primary before its roster arrives."""
    _state(monkeypatch, False, reason)

    permitted, why = fts.tunnel_serving_permitted()
    assert permitted is False
    assert why == reason


def test_an_already_allowed_machine_is_permitted(monkeypatch):
    _state(monkeypatch, True, "selected")

    assert fts.tunnel_serving_permitted() == (True, "selected")


def test_unassigned_requires_a_local_machine_identity(monkeypatch):
    """`tunnel-server-unassigned` is returned BEFORE state() validates local
    identity, so permitting it directly would let a machine with no durable
    identity start serving. Re-validated here."""
    _state(monkeypatch, False, "tunnel-server-unassigned")
    monkeypatch.setattr(fts.machine_boot, "machine_id", lambda **k: None)

    assert fts.tunnel_serving_permitted() == (False, "machine-identity-missing")


def test_unassigned_requires_the_local_machine_to_be_rostered(monkeypatch):
    """The second check state() had not yet reached: a machine absent from the
    active roster must not serve merely because no selection exists."""
    _state(monkeypatch, False, "tunnel-server-unassigned")
    monkeypatch.setattr(fts.machine_boot, "machine_id", lambda **k: "local-machine")
    monkeypatch.setattr(fts, "_personal_root_pub", lambda: "aa" * 32)
    monkeypatch.setattr(fts.fleet_roster, "load_entries", lambda **k: ())
    monkeypatch.setattr(
        fts.fleet_roster, "resolve",
        lambda entries, anchor_root_pub: {"other": type("E", (), {"machine_id": "other"})()})

    assert fts.tunnel_serving_permitted() == (False, "machine-not-rostered")


def test_unassigned_with_a_rostered_local_machine_is_permitted(monkeypatch):
    _state(monkeypatch, False, "tunnel-server-unassigned")
    monkeypatch.setattr(fts.machine_boot, "machine_id", lambda **k: "local-machine")
    monkeypatch.setattr(fts, "_personal_root_pub", lambda: "aa" * 32)
    monkeypatch.setattr(fts.fleet_roster, "load_entries", lambda **k: ())
    monkeypatch.setattr(
        fts.fleet_roster, "resolve",
        lambda entries, anchor_root_pub: {
            "local-machine": type("E", (), {"machine_id": "local-machine"})()})

    assert fts.tunnel_serving_permitted() == (True, "designation-not-required")


def test_unassigned_without_a_personal_root_is_refused(monkeypatch):
    _state(monkeypatch, False, "tunnel-server-unassigned")
    monkeypatch.setattr(fts.machine_boot, "machine_id", lambda **k: "local-machine")
    monkeypatch.setattr(fts, "_personal_root_pub", lambda: None)

    assert fts.tunnel_serving_permitted() == (False, "personal-root-missing")


def test_an_unreadable_machine_store_fails_closed(monkeypatch):
    def _raise(**kwargs):
        raise RuntimeError("machine store unreadable")

    _state(monkeypatch, False, "tunnel-server-unassigned")
    monkeypatch.setattr(fts.machine_boot, "machine_id", _raise)

    assert fts.tunnel_serving_permitted() == (False, "machine-identity-unreadable")
