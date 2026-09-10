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
    assert peer in message, "names the peer so the bad file can be found"
    # Points at the FILE, not at a Setting. See the dedicated test below for
    # why: there is no subscription row anywhere, so the first version of this
    # message sent its reader hunting one.
    assert "filenames in data/orgs/" in message, "says where the defect is"


def test_a_real_missing_peer_is_still_just_absent(caplog):
    """The pre-existing behaviour is untouched: a slug-shaped peer with no
    database is None and says nothing, because that is ordinary."""
    with caplog.at_level(logging.WARNING, logger=cross_org.__name__):
        assert cross_org.open_peer_db("definitely-not-an-org-here") is None
    assert not [r for r in caplog.records if "skipping peer" in r.getMessage()]


def test_a_stray_file_is_not_enumerated_as_a_peer(tmp_path, caplog):
    """The ROOT CAUSE, not just the crash.

    Nothing named the uuid as a peer: home has ZERO
    autonomy.org.peer-subscription rows in every store, so every read takes
    `resolve_peers` precedence 3 — "every other org slug under data/orgs/*.db"
    — and the peer list is built from FILENAMES. The stray database therefore
    became a first-class peer of every org on the machine, sorting FIRST
    because digits precede letters. Traced by host-0906-222509, 2026-09-10.

    So the fix has to be at enumeration, not only at open: a file that is not
    named like an organization is not an organization.
    """
    for name in ("anchore", "autonomy", "enterprise-ng"):
        (tmp_path / f"{name}.db").write_text("")
    for stray in (
        "2d4b90cb-1e89-452b-82cb-68ca44fd8e52",   # the one home actually has
        "None",
        "none",
    ):
        (tmp_path / f"{stray}.db").write_text("")

    with caplog.at_level(logging.WARNING, logger=cross_org.__name__):
        slugs = cross_org.list_org_slugs(root=tmp_path)

    assert slugs == ["anchore", "autonomy", "enterprise-ng"], (
        "a stray filename must not become an organization"
    )
    # And it is named, not silently dropped — once per stem.
    said = " ".join(r.getMessage() for r in caplog.records)
    assert "2d4b90cb-1e89-452b-82cb-68ca44fd8e52" in said
    assert "THE CONTENTS OF THIS DIRECTORY ARE THE REGISTRY" in said


def test_the_skip_message_does_not_name_a_subscription_row(caplog):
    """It used to say "fix the subscription row". There is no subscription row
    — zero in every store on home — so that sent its reader hunting a Setting
    that does not exist. The message must point at the filename instead."""
    with caplog.at_level(logging.WARNING, logger=cross_org.__name__):
        cross_org.open_peer_db("2d4b90cb-1e89-452b-82cb-68ca44fd8e52")
    message = " ".join(r.getMessage() for r in caplog.records)
    assert "subscription" not in message.lower()
    assert "filenames in data/orgs/" in message
