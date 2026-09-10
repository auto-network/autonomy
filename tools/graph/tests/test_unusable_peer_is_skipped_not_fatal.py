"""An unusable peer identifier must not stop a worker from starting.

`_org_db_path` refuses a slug that cannot be a filename. That guard is right,
but it fires inside `cross_org.open_peer_db`, which runs on the settings read
path reached by `SessionMonitor._broadcast_registry` — a background task at
startup. When it raised, home's dashboard could not start a NEW worker at all:

    uvicorn Started server process [1381493]
    uvicorn ERROR Application startup failed. Exiting.

The site stayed up only because the zero-downtime supervisor kept the
incumbent, which meant the machine was frozen on hours-old code and no later
commit — including the fix — could reach it. Found by host-0906-222509 within
two minutes of the guard landing, 2026-09-10.

A peer list naming a UUID is a data defect that predates the guard: home's
orgs/ carries the `2d4b90cb-….db` this path had been opening read-only all
along, with seventy replicated rows in it. The defect must stay visible and
must not be fatal.
"""

from __future__ import annotations

import logging

import pytest

from tools.graph import cross_org


@pytest.fixture(autouse=True)
def _forget_reported_peers():
    cross_org._UNUSABLE_PEERS.clear()
    yield
    cross_org._UNUSABLE_PEERS.clear()


@pytest.mark.parametrize("peer", [
    "2d4b90cb-1e89-452b-82cb-68ca44fd8e52",   # the identifier home actually had
    "c8e5cd04-8f19-4bc2-8951-a6b6b80b2699",
    None,
    "",
])
def test_an_unusable_peer_returns_none_instead_of_raising(peer):
    assert cross_org.open_peer_db(peer) is None


def test_the_skip_is_reported_once_per_process(caplog):
    peer = "2d4b90cb-1e89-452b-82cb-68ca44fd8e52"
    with caplog.at_level(logging.WARNING, logger=cross_org.__name__):
        for _ in range(5):
            assert cross_org.open_peer_db(peer) is None
    said = [r for r in caplog.records if "skipping peer" in r.getMessage()]
    assert len(said) == 1, (
        "this runs on a per-broadcast tick; repeating the warning would make "
        "it the log"
    )
    message = said[0].getMessage()
    assert peer in message, "names the peer so the bad row can be found"
    assert "fix the subscription row" in message, "says where the defect is"


def test_a_real_missing_peer_is_still_just_absent(caplog):
    """The pre-existing behaviour is untouched: a slug-shaped peer with no
    database is None and says nothing, because that is ordinary."""
    with caplog.at_level(logging.WARNING, logger=cross_org.__name__):
        assert cross_org.open_peer_db("definitely-not-an-org-here") is None
    assert not [r for r in caplog.records if "skipping peer" in r.getMessage()]
