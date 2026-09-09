"""auto-ja0rf: the per-connection lease keeper renews tunnel-wide.

Renewal is a dead-man's switch for the whole connection, so the keeper
sends ONE ``host-renew-all`` per interval regardless of how many
reservations it holds, and falls back to the legacy per-reservation
``host-renew`` only against a registry that predates the op. These are
unit tests over ``_maintain_host_leases`` with a virtual clock and a
recorded ``control()`` — no sockets.
"""

from __future__ import annotations

import asyncio

from tools.network.idkit import KeyPair
from tools.network.relaykit import connector as connector_mod

TTL = 600
_REAL_SLEEP = asyncio.sleep


class _Clock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start


def _connector() -> connector_mod.TunnelConnector:
    key = KeyPair.generate()
    return connector_mod.TunnelConnector(
        "ws://registry.invalid", "org", key, cert=object(),
                machine_key=KeyPair.generate(),
            )


def _run(desired: dict, replies, *, virtual_seconds: float,
         monkeypatch, clock=None):
    """Drive the keeper for ``virtual_seconds`` of fake time, recording
    every control op. ``replies(op, args, clock)`` returns the reply."""
    clock = clock or _Clock()
    calls: list = []
    conn = _connector()
    conn._desired_hosts = dict(desired)

    async def control(op, args, timeout=10.0):
        calls.append((op, dict(args)))
        return replies(op, args, clock)

    conn.control = control
    monkeypatch.setattr(connector_mod.time, "time", lambda: clock.now)
    deadline = clock.now + virtual_seconds

    async def fake_sleep(seconds):
        clock.now += seconds
        await _REAL_SLEEP(0)

    monkeypatch.setattr(connector_mod.asyncio, "sleep", fake_sleep)

    async def scenario():
        task = asyncio.create_task(conn._maintain_host_leases())
        try:
            for _ in range(100_000):
                if clock.now >= deadline or task.done():
                    break
                await _REAL_SLEEP(0)
            assert not task.done(), task.exception()
        finally:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    asyncio.run(scenario())
    return calls, conn


def _ok_replies(op, args, clock):
    if op == "host-register":
        return {"ok": True,
                "lease": {"generation": 1, "expires_at": clock.now + TTL}}
    if op == "host-renew-all":
        return {"ok": True, "renewed": _ok_replies.leases,
                "expires_at": clock.now + TTL}
    raise AssertionError(f"unexpected op {op}")


def test_one_keepalive_per_interval_regardless_of_reservation_count(
    monkeypatch,
):
    desired = {f"res-{i}": f"app{i}.p.serve.auto.network" for i in range(5)}
    _ok_replies.leases = 5
    calls, conn = _run(desired, _ok_replies,
                       virtual_seconds=2 * TTL, monkeypatch=monkeypatch)
    registers = [c for c in calls if c[0] == "host-register"]
    renew_alls = [c for c in calls if c[0] == "host-renew-all"]
    legacy = [c for c in calls if c[0] == "host-renew"]
    assert len(registers) == 5              # once each, at connect
    assert legacy == []                     # never per-share
    # 1200 virtual seconds at a 240-300 s renewal margin on a 600 s TTL:
    # the keeper fires a small constant number of ops, never one per
    # reservation per interval (which would be >= 10 here).
    assert 2 <= len(renew_alls) <= 5
    assert all(args == {} for _, args in renew_alls)
    # Bookkeeping followed the tunnel-wide reply.
    expiries = {l["expires_at"] for l in conn._host_leases.values()}
    assert len(expiries) == 1


def test_unknown_op_falls_back_to_per_reservation_renewal(monkeypatch):
    desired = {"res-a": "a.p.serve.auto.network",
               "res-b": "b.p.serve.auto.network"}

    def replies(op, args, clock):
        if op == "host-register":
            # A pre-auto-ja0rf registry: short 120 s leases.
            return {"ok": True,
                    "lease": {"generation": 1, "expires_at": clock.now + 120}}
        if op == "host-renew-all":
            return {"ok": False, "error": "unknown control op: 'host-renew-all'"}
        if op == "host-renew":
            return {"ok": True,
                    "lease": {"generation": 1, "expires_at": clock.now + 120}}
        raise AssertionError(op)

    calls, _ = _run(desired, replies,
                    virtual_seconds=600, monkeypatch=monkeypatch)
    renew_alls = [c for c in calls if c[0] == "host-renew-all"]
    legacy = [c for c in calls if c[0] == "host-renew"]
    assert len(renew_alls) == 1             # probed exactly once, then fell back
    assert len(legacy) >= 4                 # both leases kept alive per-share
    assert {a["reservation"] for _, a in legacy} == {"res-a", "res-b"}


def test_renewed_shortfall_triggers_full_reregistration(monkeypatch):
    desired = {"res-a": "a.p.serve.auto.network",
               "res-b": "b.p.serve.auto.network"}
    state = {"renewed": 1}                  # registry admits losing one

    def replies(op, args, clock):
        if op == "host-register":
            return {"ok": True,
                    "lease": {"generation": 2, "expires_at": clock.now + TTL}}
        if op == "host-renew-all":
            reply = {"ok": True, "renewed": state["renewed"],
                     "expires_at": clock.now + TTL}
            state["renewed"] = 2            # repaired after re-registration
            return reply
        raise AssertionError(op)

    calls, _ = _run(desired, replies,
                    virtual_seconds=TTL, monkeypatch=monkeypatch)
    registers = [c for c in calls if c[0] == "host-register"]
    # 2 at connect + 2 repair re-registrations after the shortfall reply.
    assert len(registers) == 4
