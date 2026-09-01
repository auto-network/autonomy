"""auto-g1jxw: the bounded DNS-01 challenge write path, store-backed.

Name grammar validated before any write, expiry clamped and purged
inline, per-name values capped, simultaneous apex+wildcard values
coexisting — the bounds the retired PowerDNS broker enforced, now on the
registry's own table, end to end through the real store and the
zone-state read the DNS process consumes.
"""

from __future__ import annotations

import pytest

from tools.network.registry.dns_challenges import (
    ChallengeError,
    cleanup,
    present,
    validate_challenge_name,
)
from tools.network.registry.store import RegistryStore

LABEL = "worker-9a2db2e23f1504cd0566"
NAME = f"_acme-challenge.{LABEL}.serve.auto.network"


@pytest.fixture
def store():
    return RegistryStore(":memory:")


@pytest.fixture
def tick():
    return {"now": 1_000_000}


def _now_fn(tick):
    return lambda: tick["now"]


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
def test_out_of_bound_names_are_refused(name, store, tick):
    with pytest.raises(ChallengeError):
        present(store, name, "tok", now_fn=_now_fn(tick))
    assert store.live_serve_challenges(now=tick["now"]) == {}


def test_apex_and_wildcard_values_coexist(store, tick):
    present(store, NAME, "apex-tok", now_fn=_now_fn(tick))
    present(store, NAME, "wild-tok", now_fn=_now_fn(tick))
    live = store.live_serve_challenges(now=tick["now"])
    assert live == {NAME + ".": ["apex-tok", "wild-tok"]}


def test_present_is_idempotent_and_refreshes_expiry(store, tick):
    present(store, NAME, "tok", expiry=100, now_fn=_now_fn(tick))
    tick["now"] += 90
    present(store, NAME, "tok", expiry=100, now_fn=_now_fn(tick))
    tick["now"] += 90  # past the first expiry, inside the refreshed one
    assert store.live_serve_challenges(now=tick["now"]) == {
        NAME + ".": ["tok"]
    }


def test_expired_values_purge_on_read(store, tick):
    present(store, NAME, "old", expiry=100, now_fn=_now_fn(tick))
    tick["now"] += 50
    present(store, NAME, "young", expiry=900, now_fn=_now_fn(tick))
    tick["now"] += 100
    assert store.live_serve_challenges(now=tick["now"]) == {
        NAME + ".": ["young"]
    }


def test_value_cap_refuses_the_ninth(store, tick):
    for i in range(8):
        present(store, NAME, f"v{i}", now_fn=_now_fn(tick))
    with pytest.raises(ChallengeError, match="values"):
        present(store, NAME, "one-too-many", now_fn=_now_fn(tick))


def test_cleanup_removes_one_value_keeps_sibling(store, tick):
    present(store, NAME, "a", now_fn=_now_fn(tick))
    present(store, NAME, "b", now_fn=_now_fn(tick))
    cleanup(store, NAME, "a")
    assert store.live_serve_challenges(now=tick["now"]) == {
        NAME + ".": ["b"]
    }
    cleanup(store, NAME, "b")
    assert store.live_serve_challenges(now=tick["now"]) == {}


def test_zone_state_endpoint_serves_live_challenges(client, store):
    """End to end through the HTTP surface the DNS process reads."""
    del store  # the app fixture owns its own store
    import tools.network.registry.dns_challenges as dc

    # Write through the app's store directly (the CLI path), read via HTTP.
    app_store = client.app.state.store
    dc.present(app_store, NAME, "tok",
               now_fn=lambda: client.app.state.now_fn())
    body = client.get("/v1/dns/zone-state").json()
    assert body == {"challenges": {NAME + ".": ["tok"]}}
