"""An unreachable Dolt server is not re-dialled on every call (auto-2v6ay.3).

On a node without the beads profile, each connect waited out its timeout on
the event loop: 10 s stalls every few seconds on the Windows test node
(2026-09-27). After one failed connect, calls fail at once until the backoff
passes; a success clears it; auth errors are not cached.
"""

import pymysql
import pytest

from tools.dashboard.dao import beads


@pytest.fixture
def dial(monkeypatch):
    calls = []
    monkeypatch.setattr(beads, "_conn_params", lambda org: {
        "host": "dolt", "port": 3307, "user": "u", "password": "p", "database": "d"})
    monkeypatch.setattr(beads, "_unreachable", {})
    clock = {"t": 1000.0}
    monkeypatch.setattr(beads.time, "monotonic", lambda: clock["t"])
    outcome = {"exc": pymysql.err.OperationalError(2003, "Can't connect")}

    def connect(**kw):
        calls.append(kw["host"])
        if outcome["exc"] is not None:
            raise outcome["exc"]
        return "conn"

    monkeypatch.setattr(beads.pymysql, "connect", connect)
    return calls, clock, outcome


def test_second_call_does_not_dial_while_backing_off(dial):
    calls, clock, _ = dial
    with pytest.raises(pymysql.err.OperationalError):
        beads._connect(None)
    with pytest.raises(OSError):
        beads._connect(None)
    assert calls == ["dolt"]


def test_dials_again_after_backoff_and_success_clears(dial):
    calls, clock, outcome = dial
    with pytest.raises(pymysql.err.OperationalError):
        beads._connect(None)
    clock["t"] += 31
    outcome["exc"] = None
    assert beads._connect(None) == "conn"
    assert beads._unreachable == {}
    assert calls == ["dolt", "dolt"]


def test_auth_error_is_not_cached(dial):
    calls, clock, outcome = dial
    outcome["exc"] = pymysql.err.OperationalError(1045, "Access denied")
    for _ in range(2):
        with pytest.raises(pymysql.err.OperationalError):
            beads._connect(None)
    assert calls == ["dolt", "dolt"]


def test_degraded_reader_returns_empty_without_dialling_twice(dial):
    calls, _, _ = dial
    reader = beads._degrade_when_unreachable(list)(lambda: beads._connect(None))
    assert reader() == [] and reader() == []
    assert calls == ["dolt"]
