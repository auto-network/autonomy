"""The connector executes a delegated pull; it never chooses one (auto-ew9wf).

The dashboard pulls and the connector holds the relay adapter, so the pull is
delegated as an operation. A pull runs for minutes and the control protocol is
one request/reply, so the op returns a job id immediately and the outcome is
polled.
"""

from __future__ import annotations

import asyncio
import types

import pytest

from tools.network import fleet_relay_carrier as carrier

PEER = "ab" * 32
PERSONA = "cd" * 32
SLOT = "ef" * 32


class _Refused(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _runtime(pull):
    scheduler = types.SimpleNamespace(_pull_scope=pull)
    return types.SimpleNamespace(scheduler=scheduler)


@pytest.fixture(autouse=True)
def _clean():
    carrier._RELAY_PULLS.clear()
    yield
    carrier._RELAY_PULLS.clear()


async def _start(runtime, operation_id="op-1"):
    return await carrier.start_relay_pull(
        None, runtime, peer_machine_pub=PEER, persona_pub=PERSONA,
        machine=SLOT, scope="autonomy", operation_id=operation_id,
    )


def test_it_returns_immediately_and_reports_done_after(  ):
    started = asyncio.Event()

    async def pull(machine_pub, addresses, scope, **kw):
        started.set()
        assert kw["relay_slot"] == (PERSONA, SLOT)
        assert list(addresses) == [], "a relay pull dials no direct address"

    async def run():
        reply = await _start(_runtime(pull))
        assert reply == {"ok": True, "operation_id": "op-1", "state": "running"}
        await asyncio.wait_for(started.wait(), 1)
        await carrier._RELAY_PULLS["op-1"]["task"]
        return carrier.relay_pull_status("op-1")

    status = asyncio.run(run())
    assert status["state"] == "done" and status["scope"] == "autonomy"


def test_an_unarmed_process_says_so_rather_than_pretending():
    async def run():
        return await _start(types.SimpleNamespace(scheduler=None))

    reply = asyncio.run(run())
    assert reply["ok"] is False and reply["error_kind"] == "not-armed"


def test_resubmitting_a_live_operation_does_not_start_a_second_pull():
    """One open per operation — the promise auto-ieh3l made auto-fh2nv,
    enforced here too so a caller that lost its reply cannot create a storm."""
    calls = []
    release = asyncio.Event()

    async def pull(machine_pub, addresses, scope, **kw):
        calls.append(scope)
        await release.wait()

    async def run():
        runtime = _runtime(pull)
        first = await _start(runtime)
        second = await _start(runtime)
        release.set()
        await carrier._RELAY_PULLS["op-1"]["task"]
        return first, second

    first, second = asyncio.run(run())
    assert first["state"] == "running" and second.get("duplicate") is True
    assert calls == ["autonomy"], "the second submission started nothing"


def test_a_failure_carries_the_carriers_taxonomy_across_the_socket():
    """The dashboard's controller decides stand-down vs retry vs stop from
    these fields; flattening them here would make that decision impossible on
    the far side."""
    async def pull(machine_pub, addresses, scope, **kw):
        raise _Refused("operation-already-open")

    async def run():
        await _start(_runtime(pull))
        await carrier._RELAY_PULLS["op-1"]["task"]
        return carrier.relay_pull_status("op-1")

    status = asyncio.run(run())
    assert status["state"] == "failed"
    assert status["reason"] == "operation-already-open"
    assert "Refused" in status["error"]


def test_an_unknown_operation_is_unknown_not_failed():
    """Different facts: 'never started' and 'started and failed' want
    different responses from the caller."""
    status = carrier.relay_pull_status("never-existed")
    assert status["ok"] is False
    assert status["error_kind"] == "unknown-operation"


def test_a_finished_job_is_retained_so_a_late_poll_still_learns():
    async def pull(machine_pub, addresses, scope, **kw):
        return None

    async def run():
        await _start(_runtime(pull))
        await carrier._RELAY_PULLS["op-1"]["task"]

    asyncio.run(run())
    assert carrier.relay_pull_status("op-1")["state"] == "done"
    assert carrier.RELAY_PULL_RETENTION_S >= 60, (
        "a poller must not race the reaper"
    )
