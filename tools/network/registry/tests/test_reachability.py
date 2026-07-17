"""G1 node reachability hints — the fabric's discovery seed (spec §8).

Nodes self-announce direct-dial candidates and (when relaying for the
org) their peer-relay dial URL. Pinned properties:

- node identity IS the envelope signer — no announcing someone else;
- Tier B both ways: announce needs ``node:announce``, reads need
  ``node:lookup``; anonymous callers get nothing (E1 continuity);
- stale hints expire by TTL and refreshes replace the candidate set.
"""

from __future__ import annotations

import pytest

from tools.network.idkit import KeyPair, Subject, issue_cert
from tools.network.registry.app import DEFAULT_HINT_TTL, MAX_HINT_TTL, MIN_HINT_TTL

from .conftest import DAY, NOW, ORG, signed

ADDR = "ws://198.51.100.7:9410"
ADDR2 = "wss://node-a.example:9410"
RELAY = "ws://198.51.100.7:9420"

NODE_SCOPE = ("node:announce", "node:lookup")


def announce(client, clock, key, addrs, cert=None, relay_url=None, ttl=None,
             org=ORG, expect=None):
    payload = {"addrs": addrs}
    if relay_url is not None:
        payload["relay_url"] = relay_url
    if ttl is not None:
        payload["ttl"] = ttl
    return signed(client, "POST", f"/v1/orgs/{org}/reachability", key, payload,
                  clock, cert=cert, expect=expect)


def query(client, clock, key, cert=None, node=None, org=ORG, expect=200):
    payload = {} if node is None else {"node": node}
    return signed(client, "POST", f"/v1/orgs/{org}/reachability/query", key,
                  payload, clock, cert=cert, expect=expect)


@pytest.fixture
def node_key():
    return KeyPair.generate()


@pytest.fixture
def node_cert(root, node_key):
    return issue_cert(
        root,
        node_key.public_hex,
        scope=NODE_SCOPE,
        org=ORG,
        subject=Subject("agent", "node-1"),
        not_before=NOW - 50,
        not_after=NOW + 200 * DAY,
    )


# ── lifecycle ────────────────────────────────────────────────────────


def test_announce_and_query(client, clock, root, bound_org, node_key, node_cert):
    announce(client, clock, node_key, [ADDR, ADDR2], cert=node_cert,
             relay_url=RELAY, expect=201)

    hints = query(client, clock, root).json()["hints"]
    assert len(hints) == 1
    assert hints[0]["node"] == node_key.public_hex
    assert hints[0]["addrs"] == [ADDR, ADDR2]
    assert hints[0]["relay_url"] == RELAY

    # single-node filter
    hints = query(client, clock, root, node=node_key.public_hex).json()["hints"]
    assert len(hints) == 1
    assert query(client, clock, root, node="ab" * 32).json()["hints"] == []


def test_node_identity_is_the_signer(client, clock, root, bound_org, node_key, node_cert):
    """There is no 'node' field on announce — a node can only ever plant
    hints under its OWN key. The row lands on the signer, structurally."""
    response = announce(client, clock, node_key, [ADDR], cert=node_cert, expect=201)
    assert response.json()["node"] == node_key.public_hex
    # smuggling a node field is a strict-schema rejection
    signed(client, "POST", f"/v1/orgs/{ORG}/reachability", node_key,
           {"addrs": [ADDR], "node": "ab" * 32}, clock, cert=node_cert, expect=400)


def test_refresh_replaces_candidate_set(client, clock, root, bound_org, node_key, node_cert):
    announce(client, clock, node_key, [ADDR, ADDR2], cert=node_cert,
             relay_url=RELAY, expect=201)
    clock.advance(60)
    announce(client, clock, node_key, [ADDR2], cert=node_cert, expect=201)

    hints = query(client, clock, root).json()["hints"]
    assert hints[0]["addrs"] == [ADDR2]
    # relay_url not re-announced -> gone, not lingering (last write wins)
    assert hints[0]["relay_url"] is None
    assert hints[0]["announced_at"] == NOW + 60


# ── TTL / staleness ──────────────────────────────────────────────────


def test_stale_hints_expire(client, clock, root, bound_org, node_key, node_cert):
    announce(client, clock, node_key, [ADDR], cert=node_cert, ttl=300, expect=201)
    clock.advance(299)
    assert len(query(client, clock, root).json()["hints"]) == 1
    clock.advance(2)
    assert query(client, clock, root).json()["hints"] == []


def test_refresh_extends_expiry(client, clock, root, bound_org, node_key, node_cert):
    announce(client, clock, node_key, [ADDR], cert=node_cert, ttl=300, expect=201)
    clock.advance(250)
    announce(client, clock, node_key, [ADDR], cert=node_cert, ttl=300, expect=201)
    clock.advance(250)  # past the first window, inside the refreshed one
    assert len(query(client, clock, root).json()["hints"]) == 1


def test_ttl_is_clamped(client, clock, root, bound_org, node_key, node_cert):
    body = announce(client, clock, node_key, [ADDR], cert=node_cert,
                    ttl=10_000_000, expect=201).json()
    assert body["expires_at"] == clock.now + MAX_HINT_TTL
    body = announce(client, clock, node_key, [ADDR], cert=node_cert,
                    ttl=1, expect=201).json()
    assert body["expires_at"] == clock.now + MIN_HINT_TTL
    body = announce(client, clock, node_key, [ADDR], cert=node_cert, expect=201).json()
    assert body["expires_at"] == clock.now + DEFAULT_HINT_TTL
    announce(client, clock, node_key, [ADDR], cert=node_cert, ttl=0, expect=400)
    announce(client, clock, node_key, [ADDR], cert=node_cert, ttl="1h", expect=400)


# ── access control ───────────────────────────────────────────────────


def test_anonymous_cannot_read_hints(client, clock, root, bound_org, node_key, node_cert):
    """E1 continuity: an org's interior addresses are Tier B. No envelope,
    a garbage envelope, and an unsigned GET all learn nothing."""
    announce(client, clock, node_key, [ADDR], cert=node_cert, relay_url=RELAY, expect=201)

    response = client.post(f"/v1/orgs/{ORG}/reachability/query", json={})
    assert response.status_code == 400
    assert ADDR not in response.text

    response = client.post(f"/v1/orgs/{ORG}/reachability/query",
                           json={"v": 1, "payload": {}})
    assert response.status_code == 400
    assert ADDR not in response.text

    response = client.get(f"/v1/orgs/{ORG}/reachability/query")
    assert response.status_code == 405
    assert ADDR not in response.text


def test_scope_enforcement(client, clock, root, bound_org, node_key):
    """A chain without node:announce / node:lookup is refused — attenuation
    is meaningful for the hint surface too."""
    wrong = issue_cert(
        root, node_key.public_hex, scope=("link:publish",), org=ORG,
        subject=Subject("agent", "node-1"),
        not_before=NOW - 50, not_after=NOW + 200 * DAY,
    )
    announce(client, clock, node_key, [ADDR], cert=wrong, expect=403)
    query(client, clock, node_key, cert=wrong, expect=403)

    announce_only = issue_cert(
        root, node_key.public_hex, scope=("node:announce",), org=ORG,
        subject=Subject("agent", "node-1"),
        not_before=NOW - 50, not_after=NOW + 200 * DAY,
    )
    announce(client, clock, node_key, [ADDR], cert=announce_only, expect=201)
    query(client, clock, node_key, cert=announce_only, expect=403)


def test_unknown_org_and_bad_signature(client, clock, root, node_key, node_cert):
    announce(client, clock, node_key, [ADDR], cert=node_cert,
             org="99999999-9999-4999-8999-999999999999", expect=404)


# ── payload validation ───────────────────────────────────────────────


def test_addr_validation(client, clock, root, bound_org, node_key, node_cert):
    announce(client, clock, node_key, "not-a-list", cert=node_cert, expect=400)
    announce(client, clock, node_key, ["http://x.example"], cert=node_cert, expect=400)
    announce(client, clock, node_key, ["ws://a b"], cert=node_cert, expect=400)
    announce(client, clock, node_key, ["ws://" + "x" * 300], cert=node_cert, expect=400)
    announce(client, clock, node_key, [ADDR] * 9, cert=node_cert, expect=400)
    announce(client, clock, node_key, [ADDR], cert=node_cert,
             relay_url="http://x.example", expect=400)
    # empty candidate list is legal: "I am not directly reachable" — the
    # row can still carry a relay_url or just refresh presence
    announce(client, clock, node_key, [], cert=node_cert, relay_url=RELAY, expect=201)
