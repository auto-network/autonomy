"""auto-g1jxw: the DNS-01 challenge broker — TXT-only, name-bounded,
atomic multi-value RRset writes, expiry-ledgered cleanup.

The broker is the ONLY automated mutation path into the delegated zone
and structurally cannot write outside `_acme-challenge.<persona-label>.
serve.auto.network` TXT RRsets. Its argv/exit contract is the frozen
seam auto-bhs3c bridges to; these tests drive the same core through a
fake PowerDNS API client.
"""

from __future__ import annotations

import json

import pytest

from tools.network.estate.dns.challenge_broker import (
    BrokerError,
    ChallengeBroker,
    validate_challenge_name,
)

LABEL = "worker-9a2db2e23f1504cd0566"
NAME = f"_acme-challenge.{LABEL}.serve.auto.network"


class FakeClient:
    """In-memory PowerDNS API: full-RRset replacement only."""

    def __init__(self):
        self.rrsets: dict[str, list[str]] = {}
        self.patches: list[tuple[str, str, list[str]]] = []

    def get_txt(self, name: str) -> list[str]:
        return list(self.rrsets.get(name, []))

    def replace_txt(self, name: str, values: list[str], ttl: int) -> None:
        self.patches.append((name, "REPLACE", list(values)))
        if values:
            self.rrsets[name] = list(values)
        else:
            self.rrsets.pop(name, None)


@pytest.fixture
def broker(tmp_path):
    client = FakeClient()
    clock = {"now": 1_000_000}
    b = ChallengeBroker(
        client, ledger_path=tmp_path / "challenges.json",
        now_fn=lambda: clock["now"],
    )
    return b, client, clock


# -- name boundary ----------------------------------------------------------


def test_valid_challenge_name_passes():
    assert validate_challenge_name(NAME) == LABEL


@pytest.mark.parametrize("name", [
    "serve.auto.network",
    "*.serve.auto.network",
    f"{LABEL}.serve.auto.network",                       # no prefix
    f"_acme-challenge.{LABEL}.auto.network",             # parent zone
    "_acme-challenge.ns1.serve.auto.network.evil.com",   # suffix spoof
    f"_acme-challenge.sub.{LABEL}.serve.auto.network",   # extra label
    "_acme-challenge..serve.auto.network",
    "_acme-challenge.UPPER-aa.serve.auto.network",
    "_acme-challenge." + "a" * 64 + ".serve.auto.network",
])
def test_out_of_bound_names_are_refused(name):
    with pytest.raises(BrokerError):
        validate_challenge_name(name)


# -- present ----------------------------------------------------------------


def test_present_writes_one_atomic_txt_rrset(broker):
    b, client, _ = broker
    b.present(NAME, "token-value-1")
    assert client.rrsets[NAME] == ['"token-value-1"']
    assert client.patches == [(NAME, "REPLACE", ['"token-value-1"'])]


def test_simultaneous_apex_and_wildcard_values_coexist(broker):
    """Two live TXT values at one challenge name (apex + wildcard ACME
    authorizations) — the second present preserves the first."""
    b, client, _ = broker
    b.present(NAME, "apex-token")
    b.present(NAME, "wildcard-token")
    assert sorted(client.rrsets[NAME]) == ['"apex-token"', '"wildcard-token"']


def test_present_is_idempotent_per_value(broker):
    b, client, _ = broker
    b.present(NAME, "tok")
    b.present(NAME, "tok")
    assert client.rrsets[NAME] == ['"tok"']


def test_ttl_is_clamped_and_value_count_bounded(broker):
    b, client, _ = broker
    b.present(NAME, "t", ttl=5)          # below floor → clamped
    assert b.last_ttl == 60
    b.present(NAME, "t2", ttl=9999)      # above ceiling → clamped
    assert b.last_ttl == 300
    for i in range(6):
        b.present(NAME, f"v{i}")
    with pytest.raises(BrokerError, match="value"):
        b.present(NAME, "one-too-many")


def test_non_txt_and_foreign_names_never_reach_the_api(broker):
    b, client, _ = broker
    with pytest.raises(BrokerError):
        b.present("registry.auto.network", "x")
    assert client.patches == []


# -- cleanup / expiry -------------------------------------------------------


def test_cleanup_removes_one_value_and_empty_rrset(broker):
    b, client, _ = broker
    b.present(NAME, "a")
    b.present(NAME, "b")
    b.cleanup(NAME, "a")
    assert client.rrsets[NAME] == ['"b"']
    b.cleanup(NAME, "b")
    assert NAME not in client.rrsets


def test_purge_expired_removes_only_overdue_values(broker):
    b, client, clock = broker
    b.present(NAME, "old", expiry=100)
    clock["now"] += 50
    b.present(NAME, "young", expiry=900)
    clock["now"] += 100  # "old" is now past expiry, "young" is not
    purged = b.purge_expired()
    assert purged == 1
    assert client.rrsets[NAME] == ['"young"']
    assert b.purge_expired() == 0  # idempotent


def test_ledger_reconciles_against_live_zone(broker, tmp_path):
    """A value in the ledger but absent from the zone (someone cleaned it
    by hand) is dropped from the ledger, not re-created or errored."""
    b, client, clock = broker
    b.present(NAME, "gone", expiry=100)
    client.rrsets.pop(NAME)          # out-of-band removal
    clock["now"] += 1_000
    assert b.purge_expired() == 0    # nothing to remove from the zone
    ledger = json.loads((tmp_path / "challenges.json").read_text())
    assert ledger == {}


def test_ledger_survives_restart(broker, tmp_path):
    b, client, clock = broker
    b.present(NAME, "persist", expiry=100)
    clock["now"] += 1_000
    reborn = ChallengeBroker(
        client, ledger_path=tmp_path / "challenges.json",
        now_fn=lambda: clock["now"],
    )
    assert reborn.purge_expired() == 1
    assert NAME not in client.rrsets
