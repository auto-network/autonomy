"""F3 broker surface — per-org topics: head hints + encrypted mailbox.

The registry is T0-blind here (L6): endpoints accept hashes and opaque
ciphertext only, reject anything else structurally, and gate every call
(publish AND subscribe — Tier B) through the I4 chain with the exact
per-topic scope ``topic:<name>``.
"""

from __future__ import annotations

import base64

import pytest

from tools.network.idkit import KeyPair, Subject, issue_cert

from .conftest import DAY, NOW, ORG, signed

TOPIC = "authority"
CONTENT = "content.notes"
H1 = "11" * 32
H2 = "22" * 32
H3 = "33" * 32
CT = base64.b64encode(b"\x93" * 64).decode("ascii")
CT_BIG = base64.b64encode(b"\x93" * 128).decode("ascii")


def topic_path(topic, suffix, org=ORG):
    return f"/v1/orgs/{org}/topics/{topic}/{suffix}"


def publish(client, clock, key, heads, topic=TOPIC, cert=None, org=ORG, expect=None):
    return signed(client, "POST", topic_path(topic, "heads", org), key,
                  {"heads": heads}, clock, cert=cert, expect=expect)


def poll(client, clock, key, since=0, topic=TOPIC, cert=None, expect=200):
    return signed(client, "POST", topic_path(topic, "heads/poll"), key,
                  {"since": since}, clock, cert=cert, expect=expect)


def deposit(client, clock, key, hashes, ciphertext=CT, topic=TOPIC, cert=None,
            expect=None, v=1):
    return signed(client, "POST", topic_path(topic, "bundles"), key,
                  {"v": v, "hashes": hashes, "ciphertext": ciphertext}, clock,
                  cert=cert, expect=expect)


def fetch(client, clock, key, payload=None, topic=TOPIC, cert=None, expect=200):
    return signed(client, "POST", topic_path(topic, "bundles/fetch"), key,
                  payload or {}, clock, cert=cert, expect=expect)


@pytest.fixture
def sync_key():
    return KeyPair.generate()


@pytest.fixture
def sync_cert(root, sync_key):
    """Delegated syncer: may touch the authority topic and nothing else."""
    return issue_cert(
        root,
        sync_key.public_hex,
        scope=(f"topic:{TOPIC}",),
        org=ORG,
        subject=Subject("agent", "sync-1"),
        not_before=NOW - 50,
        not_after=NOW + 200 * DAY,
    )


# ── hints: publish + poll fanout ─────────────────────────────────────


def test_publish_and_poll_hints(client, clock, root, bound_org):
    assert publish(client, clock, root, [H1], expect=201).json()["seq"] == 1
    clock.advance(5)
    assert publish(client, clock, root, [H1, H2], expect=201).json()["seq"] == 2

    body = poll(client, clock, root).json()
    assert [h["seq"] for h in body["hints"]] == [1, 2]
    assert body["hints"][0]["heads"] == [H1]
    assert body["latest"]["heads"] == [H1, H2]
    assert body["next_since"] == 2

    # cursor semantics: only news after the cursor, latest always present
    body = poll(client, clock, root, since=2).json()
    assert body["hints"] == [] and body["latest"]["seq"] == 2
    assert body["next_since"] == 2

    # re-announcing the identical head set is a no-op (heartbeat pushes
    # from a quiet org must not grow the hint stream)
    assert publish(client, clock, root, [H1, H2], expect=201).json()["seq"] == 2
    assert poll(client, clock, root).json()["next_since"] == 2


def test_topics_are_isolated(client, clock, root, bound_org):
    publish(client, clock, root, [H1], topic=TOPIC, expect=201)
    deposit(client, clock, root, [H1], topic=TOPIC, expect=201)

    body = poll(client, clock, root, topic=CONTENT).json()
    assert body["hints"] == [] and body["latest"] is None
    assert fetch(client, clock, root, topic=CONTENT).json()["bundles"] == []


# ── mailbox: deposit + fetch ─────────────────────────────────────────


def test_mailbox_store_and_forward(client, clock, root, bound_org):
    deposit(client, clock, root, [H1, H2], expect=201)
    clock.advance(5)
    deposit(client, clock, root, [H3], ciphertext=CT_BIG, expect=201)

    body = fetch(client, clock, root).json()
    assert [b["seq"] for b in body["bundles"]] == [1, 2]
    assert body["bundles"][0]["hashes"] == [H1, H2]
    assert body["bundles"][0]["size"] == 64
    assert base64.b64decode(body["bundles"][0]["ciphertext"]) == b"\x93" * 64
    assert body["next_since"] == 2

    # store-and-forward: the same rows serve any later (offline) reader
    later = fetch(client, clock, root, {"since": 1}).json()
    assert [b["seq"] for b in later["bundles"]] == [2]

    meta = fetch(client, clock, root, {"meta_only": True}).json()
    assert all("ciphertext" not in b for b in meta["bundles"])
    assert [b["hashes"] for b in meta["bundles"]] == [[H1, H2], [H3]]

    want = fetch(client, clock, root, {"want": [H3]}).json()
    assert [b["seq"] for b in want["bundles"]] == [2]
    assert want["bundles"][0]["v"] == 1  # version stored and returned verbatim
    # a want-mode match set is not a mailbox position: no cursor
    assert "next_since" not in want
    none = fetch(client, clock, root, {"want": ["ee" * 32]}).json()
    assert none["bundles"] == []


# ── authz: Tier B, per-topic scope ───────────────────────────────────


def test_topic_calls_require_a_chain_to_the_bound_root(client, clock, bound_org):
    stranger = KeyPair.generate()
    publish(client, clock, stranger, [H1], expect=403)
    deposit(client, clock, stranger, [H1], expect=403)
    poll(client, clock, stranger, expect=403)
    fetch(client, clock, stranger, expect=403)


def test_delegated_scope_is_per_topic(client, clock, bound_org, sync_key, sync_cert):
    # exact scope: full access to the authority topic...
    publish(client, clock, sync_key, [H1], cert=sync_cert, expect=201)
    deposit(client, clock, sync_key, [H1], cert=sync_cert, expect=201)
    poll(client, clock, sync_key, cert=sync_cert)
    fetch(client, clock, sync_key, cert=sync_cert)
    # ...and none to any other topic, read or write (403 ScopeError)
    publish(client, clock, sync_key, [H1], topic=CONTENT, cert=sync_cert, expect=403)
    deposit(client, clock, sync_key, [H1], topic=CONTENT, cert=sync_cert, expect=403)
    poll(client, clock, sync_key, topic=CONTENT, cert=sync_cert, expect=403)
    fetch(client, clock, sync_key, topic=CONTENT, cert=sync_cert, expect=403)


def test_link_scope_does_not_reach_topics(client, clock, bound_org, session_key,
                                          session_cert):
    """SESSION_SCOPE certs (link/tunnel powers) hold no topic scope."""
    publish(client, clock, session_key, [H1], cert=session_cert, expect=403)
    poll(client, clock, session_key, cert=session_cert, expect=403)


def test_root_direct_reaches_every_topic(client, clock, root, bound_org):
    publish(client, clock, root, [H1], topic=CONTENT, expect=201)
    poll(client, clock, root, topic=CONTENT)


def test_unknown_org_is_404(client, clock, root):
    publish(client, clock, root, [H1], org="99999999-9999-4999-8999-999999999999",
            expect=404)


# ── structural validation (the L6 shape gate) ────────────────────────


def test_bundle_payload_shape_is_strict(client, clock, root, bound_org):
    path = topic_path(TOPIC, "bundles")
    # plaintext smuggling fields are rejected outright
    signed(client, "POST", path, root,
           {"v": 1, "hashes": [H1], "ciphertext": CT, "events": ["plaintext"]},
           clock, expect=400)
    signed(client, "POST", path, root, {"v": 1, "hashes": [H1]}, clock, expect=400)
    # version must be declared and supported
    signed(client, "POST", path, root, {"hashes": [H1], "ciphertext": CT},
           clock, expect=400)
    deposit(client, clock, root, [H1], v=2, expect=400)
    # ciphertext must be real base64 of at least one AEAD blob
    deposit(client, clock, root, [H1], ciphertext="not base64!!", expect=400)
    deposit(client, clock, root, [H1], ciphertext="", expect=400)
    too_small = base64.b64encode(b"\x93" * 27).decode("ascii")  # < nonce+tag
    deposit(client, clock, root, [H1], ciphertext=too_small, expect=400)
    # hashes must be sorted unique 64-hex
    deposit(client, clock, root, [H2, H1], expect=400)
    deposit(client, clock, root, [H1, H1], expect=400)
    deposit(client, clock, root, ["ZZ" * 32], expect=400)
    deposit(client, clock, root, [], expect=400)
    # $-anchored regexes would admit a trailing newline; \Z must not
    deposit(client, clock, root, ["a" * 64 + "\n"], expect=400)


def test_hint_and_fetch_validation(client, clock, root, bound_org):
    publish(client, clock, root, ["short"], expect=400)
    signed(client, "POST", topic_path(TOPIC, "heads"), root, {}, clock, expect=400)
    signed(client, "POST", topic_path(TOPIC, "heads/poll"), root,
           {"since": -1}, clock, expect=400)
    signed(client, "POST", topic_path(TOPIC, "bundles/fetch"), root,
           {"since": 0, "want": [H1]}, clock, expect=400)
    signed(client, "POST", topic_path(TOPIC, "bundles/fetch"), root,
           {"meta_only": "yes"}, clock, expect=400)


def test_topic_name_is_validated(client, clock, root, bound_org):
    publish(client, clock, root, [H1], topic="UPPER", expect=400)
    publish(client, clock, root, [H1], topic="-leading", expect=400)
    publish(client, clock, root, [H1], topic="x" * 65, expect=400)
    publish(client, clock, root, [H1], topic="abc%0Adef", expect=400)


def test_org_reclaim_wipes_topic_state(client, clock, bound_org_none):
    """When an expired binding is reclaimed by a new key, the previous
    org's hint stream and encrypted mailbox must not survive into the
    new org's topics (their seq counters must reset too)."""
    from tools.network.idkit import KeyPair

    from .conftest import ORG_NONE, register

    old_root = bound_org_none
    publish(client, clock, old_root, [H1], org=ORG_NONE, expect=201)
    signed(client, "POST", topic_path(TOPIC, "bundles", ORG_NONE), old_root,
           {"v": 1, "hashes": [H1], "ciphertext": CT}, clock, expect=201)

    clock.advance(31 * 86_400)  # binding expires
    new_root = KeyPair.generate()
    assert register(client, clock, new_root, org_uuid=ORG_NONE).status_code == 201

    body = signed(client, "POST", topic_path(TOPIC, "heads/poll", ORG_NONE),
                  new_root, {"since": 0}, clock, expect=200).json()
    assert body["hints"] == [] and body["latest"] is None
    body = signed(client, "POST", topic_path(TOPIC, "bundles/fetch", ORG_NONE),
                  new_root, {}, clock, expect=200).json()
    assert body["bundles"] == []
    # and the new org's first rows start at seq 1, not after the old org's
    assert signed(client, "POST", topic_path(TOPIC, "heads", ORG_NONE), new_root,
                  {"heads": [H2]}, clock, expect=201).json()["seq"] == 1


def test_protocol_caps_are_cross_pinned():
    """One 64-head / 4096-hash grammar across client, broker, and schema —
    whoever bumps one constant must bump them together."""
    from tools.graph.schemas import network_ledger as schema
    from tools.network.ledger import MAX_BUNDLE_EVENTS
    from tools.network.ledger.broker import MAX_DEPOSIT_BYTES, MAX_WANT_HASHES
    from tools.network.ledger.bundles import _NONCE_LEN
    from tools.network.ledger.sync import MAX_SYNC_HEADS
    from tools.network.registry.app import (
        MAX_BUNDLE_BYTES,
        MAX_BUNDLE_HASHES,
        MAX_HINT_HEADS,
        MIN_BUNDLE_BYTES,
    )
    from tools.network.registry.witness import MAX_WITNESS_HEADS

    assert MAX_SYNC_HEADS == MAX_HINT_HEADS == MAX_WITNESS_HEADS == 64  # 64 also pinned in the
    # ledger-state Settings schema's _require_heads (asserted via source)
    import inspect

    assert "64" in inspect.getsource(schema._require_heads)
    assert MAX_BUNDLE_EVENTS == MAX_BUNDLE_HASHES == MAX_WANT_HASHES
    assert MAX_DEPOSIT_BYTES == MAX_BUNDLE_BYTES
    assert MIN_BUNDLE_BYTES == _NONCE_LEN + 16
