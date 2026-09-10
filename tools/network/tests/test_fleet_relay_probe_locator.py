"""The probe proves the peer the locator names, instead of guessing the roster.

auto-e38g4. Without a locator the probe tries each roster machine in turn until
the fleet handshake proves one, paying a connect and a handshake round trip per
wrong guess. A peer's verified descriptor already says which durable key serves
that slot, so the probe proves exactly one and reports where it learned it.

The locator decides nothing about membership (contract graph://7ed8a519-356
§1, §4): it selects WHICH key to prove, and the handshake still proves it.
"""

from __future__ import annotations

import asyncio

import pytest

from tools.network import fleet_relay_carrier as carrier

ORG = "11111111-1111-4111-8111-111111111111"
PERSONA = "ab" * 32

OWN_DURABLE = "11" * 32
OWN_SLOT = "aa" * 32
PEER_DURABLE = "33" * 32
PEER_SLOT = "cc" * 32
#: Two more rostered machines, ordered ahead of the peer, so a roster scan has
#: something to be wrong about.
DECOY_ONE = "22" * 32
DECOY_TWO = "44" * 32


class _Connector:
    serving_slot = {"persona_pub": PERSONA, "machine": OWN_SLOT}


class _Entry:
    def __init__(self, pub):
        self.machine_pub = pub


class _Authenticator:
    machine_pub = OWN_DURABLE
    root_pub = "99" * 32

    def _roster_entries(self):
        return ()


class _Scheduler:
    authenticator = _Authenticator()


class _Runtime:
    scheduler = _Scheduler()


class _Channel:
    async def close(self):
        return None


@pytest.fixture
def probe(monkeypatch):
    """A probe whose transport succeeds only for the truly-serving peer, and
    records every key it was asked to prove."""
    asked = []

    async def connect(connector, persona_pub, machine, *, authenticator,
                      expected_machine_pub, claimed_machine_pub=None, timeout=10.0):
        asked.append(expected_machine_pub)
        if expected_machine_pub != PEER_DURABLE:
            raise ConnectionError("handshake refused: wrong durable peer")
        return _Channel()

    monkeypatch.setattr(carrier, "fleet_relay_connect", connect)
    # Ordered so the real peer is LAST: a roster scan must visit the decoys.
    monkeypatch.setattr(
        carrier, "resolve_roster",
        lambda entries, anchor_root_pub: {
            OWN_DURABLE: None, DECOY_ONE: None, DECOY_TWO: None, PEER_DURABLE: None,
        },
    )

    def run(**kwargs):
        return asyncio.run(carrier.relay_probe(
            _Connector(), _Runtime(),
            targets=[{"persona_pub": PERSONA, "machine": PEER_SLOT}],
            timeout=1.0, **kwargs,
        )), asked

    return run


def _locators(**over):
    locator = {
        "relay_base": "wss://auto.network",
        "org_uuid": ORG,
        "persona_pub": PERSONA,
        "serving_machine_pub": PEER_SLOT,
    }
    locator.update(over)
    return {PEER_DURABLE: locator}


def test_without_a_locator_the_probe_guesses_the_roster(probe):
    result, asked = probe()

    (record,) = result["results"]
    assert record["ok"] is True and record["durable_peer"] == PEER_DURABLE
    assert record["source"] == "roster"
    # The cost of guessing, made visible: two refused handshakes before the
    # right key, each reported as an attempt.
    assert [a["expected"] for a in record["attempts"]] == [DECOY_ONE, DECOY_TWO]
    assert asked == [DECOY_ONE, DECOY_TWO, PEER_DURABLE]


def test_with_a_locator_the_probe_proves_one_key_and_says_so(probe):
    result, asked = probe(locators=_locators())

    (record,) = result["results"]
    assert record["ok"] is True and record["durable_peer"] == PEER_DURABLE
    assert record["source"] == "descriptor"
    assert record["attempts"] == []
    assert asked == [PEER_DURABLE]
    assert result["locators"] == 1


def test_a_locator_naming_the_wrong_slot_is_simply_not_used(probe):
    """It maps a slot this probe never visits, so the visited slot falls back
    to the roster scan rather than to a wrong identity."""
    result, asked = probe(locators=_locators(serving_machine_pub="ee" * 32))

    (record,) = result["results"]
    assert record["source"] == "roster"
    assert record["ok"] is True and asked[-1] == PEER_DURABLE


def test_a_locator_for_a_machine_that_left_the_roster_is_ignored(probe):
    """Membership is the roster's answer. A locator from a machine that is no
    longer rostered resolves to nothing rather than to a stale identity."""
    gone = "77" * 32
    result, asked = probe(locators={gone: _locators()[PEER_DURABLE]})

    (record,) = result["results"]
    assert record["source"] == "roster"
    assert result["locators"] == 0
    assert asked == [DECOY_ONE, DECOY_TWO, PEER_DURABLE]


def test_a_locator_that_names_the_wrong_peer_fails_rather_than_admits(
    probe, monkeypatch
):
    """The whole safety argument in one test: a locator can misdirect the probe
    but the handshake still decides, so a wrong locator produces a failed probe
    and never a false pass."""
    result, asked = probe(
        locators={DECOY_ONE: _locators()[PEER_DURABLE]},
    )

    (record,) = result["results"]
    assert record["source"] == "descriptor"
    assert record["ok"] is False
    assert asked == [DECOY_ONE]
    assert record["attempts"][0]["expected"] == DECOY_ONE
