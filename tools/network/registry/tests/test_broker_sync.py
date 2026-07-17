"""F3 acceptance — ledger sync through the broker mailbox (spec §6–7).

End-to-end over the real HTTP surface: two LedgerStore replicas diverge
offline, reconcile through the registry's encrypted mailbox, and
converge to the same fold. Pinned here:

- **Convergence** via the broker path (peer path is pinned in
  ``tools/network/ledger/tests/test_sync.py``).
- **L6**: after a full sync round, the registry's on-disk state contains
  no plaintext event bytes — scanned against every event wire and every
  ledger vocabulary marker.
- **Plane separation**: an up-to-date replica polling the hint stream
  never touches the bundle mailbox.
- **Offline catch-up**: a node that slept through N rounds drains the
  mailbox in ONE ``broker_pull`` call.
- **Topic scoping**: an authority-topic subscriber never receives
  content bundles — by delivery, by authz scope, and by AAD.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tools.network.idkit import KeyPair, Subject, issue_cert
from tools.network.ledger import BundleError, derive_sync_key, open_bundle
from tools.network.ledger.broker import (
    AUTHORITY_TOPIC,
    BrokerClient,
    BrokerCursor,
    BrokerError,
    broker_pull,
    broker_push,
)
from tools.network.ledger.events import EVENT_TYPES
from tools.network.ledger.testkit import OrgSim
from tools.network.registry.app import create_app

from .conftest import DAY, NOW, ORG, Clock, register

CONTENT_TOPIC = "content.notes"


class Org(OrgSim):
    """The shared testkit org, bound to the registry ORG uuid + sync key."""

    def __init__(self):
        super().__init__(ORG)
        self.sync_key = derive_sync_key(self.genesis)


class Recorder:
    """TestClient transport that logs every (method, path) it carries."""

    def __init__(self, client: TestClient):
        self.client = client
        self.calls = []

    def __call__(self, method: str, path: str, body: dict):
        self.calls.append((method, path))
        response = self.client.request(method, path, json=body)
        return response.status_code, response.json()

    def paths(self, suffix: str) -> list:
        return [p for _m, p in self.calls if p.endswith(suffix)]


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def org():
    return Org()


@pytest.fixture
def registry_db(tmp_path):
    return str(tmp_path / "registry.db")


@pytest.fixture
def harness(clock, org, registry_db):
    """Registry app on disk + bound org + a transport recorder."""
    app = create_app(registry_db, now_fn=clock)
    client = TestClient(app)
    assert register(client, clock, org.root).status_code == 201
    return Recorder(client)


def broker_for(harness, org, clock, key=None, cert=None) -> BrokerClient:
    return BrokerClient(
        harness, ORG, key or org.root, cert=cert, now_fn=lambda: clock.now
    )


# ── acceptance: diverge → broker mailbox → converged ─────────────────


def test_diverged_replicas_converge_via_broker(harness, org, clock):
    a, b = org.store(), org.store()
    for _ in range(3):
        org.delegate(a)
    for _ in range(2):
        org.delegate(b)
    assert a.fold().fingerprint() != b.fold().fingerprint()

    broker = broker_for(harness, org, clock)
    # each node mails its delta and announces; then each drains the mailbox
    assert broker_push(a, broker, org.sync_key) == 4  # genesis + 3
    assert broker_push(b, broker, org.sync_key) == 2  # its own 2 only (anti-entropy)
    pulled_a, _ = broker_pull(a, broker, org.sync_key)
    pulled_b, _ = broker_pull(b, broker, org.sync_key)

    assert pulled_a == 2 and pulled_b == 3
    assert a.fold().fingerprint() == b.fold().fingerprint()
    assert a.ledger.all_ids() == b.ledger.all_ids()
    assert a.fold().valid == b.fold().valid


def test_l6_broker_disk_holds_no_plaintext_events(harness, org, clock, registry_db):
    """Scan the registry's on-disk bytes after a full sync round: no event
    wire, no fragment of one, no ledger vocabulary marker."""
    a, b = org.store(), org.store()
    org.delegate(a, scope=("invite:member", "link:publish"))
    org.delegate(b, scope=("role:grant:member",))
    broker = broker_for(harness, org, clock)
    broker_push(a, broker, org.sync_key)
    broker_push(b, broker, org.sync_key)
    broker_pull(a, broker, org.sync_key)
    broker_pull(b, broker, org.sync_key)
    assert a.fold().fingerprint() == b.fold().fingerprint()

    # flush WAL so the scan sees every byte sqlite ever wrote
    store = harness.client.app.state.store
    store._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    disk = b""
    for suffix in ("", "-wal", "-shm"):
        try:
            with open(registry_db + suffix, "rb") as fh:
                disk += fh.read()
        except FileNotFoundError:
            pass
    assert disk, "registry wrote nothing to disk?"

    for event in a.events():
        wire = event.to_json()
        assert wire not in disk, "full event wire found in broker storage"
        assert event.sig.encode() not in disk, "event signature leaked to broker"
        assert event.event_id.encode() in disk  # hashes ARE the L6 allowance
        if event.type == "delegate":
            # payload-only material: grantee keys and scope strings exist
            # nowhere but inside event payloads — none may reach the disk
            assert event.payload["child_pub"].encode() not in disk
            for scope in event.payload["scope"]:
                assert scope.encode() not in disk
    for marker in [b'"type"', b'"payload"', b'"author_key"', b'"parents"'] + [
        f'"{t}"'.encode() for t in EVENT_TYPES
    ]:
        assert marker not in disk, f"ledger marker {marker!r} in broker storage"


def test_up_to_date_replica_never_touches_the_data_plane(harness, org, clock):
    a, b = org.store(), org.store()
    org.delegate(a)
    broker = broker_for(harness, org, clock)
    broker_push(a, broker, org.sync_key)
    _, cursor_b = broker_pull(b, broker, org.sync_key)
    assert b.fold().fingerprint() == a.fold().fingerprint()

    harness.calls.clear()
    pulled, cursor_b = broker_pull(b, broker, org.sync_key, cursor=cursor_b)
    assert pulled == 0
    assert harness.paths("heads/poll"), "hint stream must be consulted"
    assert not harness.paths("bundles/fetch"), (
        "no new heads announced, yet the data plane moved (§7 separation)"
    )


def test_offline_node_drains_mailbox_in_one_pull(harness, org, clock):
    live, offline = org.store(), org.store()
    broker = broker_for(harness, org, clock)
    rounds = 8
    for _ in range(rounds):  # N rounds of activity, each pushed while offline sleeps
        org.delegate(live)
        broker_push(live, broker, org.sync_key)
        clock.advance(60)

    harness.calls.clear()
    pulled, _ = broker_pull(offline, broker, org.sync_key)

    assert pulled == rounds
    assert offline.fold().fingerprint() == live.fold().fingerprint()
    assert len(harness.paths("bundles/fetch")) <= 2, "catch-up must be one pass"


def test_fresh_node_bootstraps_from_mailbox(harness, org, clock):
    """A joiner holding only genesis (the org identity + sync key source)
    reconstructs the whole replica from the mailbox."""
    live = org.store()
    for _ in range(4):
        org.delegate(live)
    broker = broker_for(harness, org, clock)
    broker_push(live, broker, org.sync_key)

    joiner = org.store()  # genesis only
    pulled, _ = broker_pull(joiner, broker, org.sync_key)
    assert pulled == 4
    assert joiner.fold().fingerprint() == live.fold().fingerprint()


# ── topic scoping: authority vs content ──────────────────────────────


def test_authority_subscriber_never_receives_content_bundles(harness, org, clock):
    """The mandatory authority topic and opt-in content topics are
    separate mailboxes: subscribing to authority alone moves no content
    bundle, no content hint, no content ciphertext (§7 interest scoping)."""
    a = org.store()
    org.delegate(a)
    broker = broker_for(harness, org, clock)
    broker_push(a, broker, org.sync_key)
    # a content stream also flows through the broker, same org
    content = org.store()
    org.delegate(content)
    org.delegate(content)
    broker_push(content, broker, org.sync_key, topic=CONTENT_TOPIC)

    subscriber = org.store()
    harness.calls.clear()
    broker_pull(subscriber, broker, org.sync_key)  # authority only

    assert all(f"/topics/{AUTHORITY_TOPIC}/" in p for _m, p in harness.calls), (
        "authority-only subscriber touched another topic's endpoints"
    )
    assert subscriber.ledger.all_ids() == a.ledger.all_ids()
    hints, latest, _ = broker.poll_hints(AUTHORITY_TOPIC)
    content_only = content.ledger.all_ids() - a.ledger.all_ids()
    for hint in hints + ([latest] if latest else []):
        assert not (set(hint["heads"]) & content_only), (
            "content heads leaked into the authority hint stream"
        )


def test_content_bundle_cannot_splice_into_authority_topic(harness, org, clock):
    """Even a dishonest broker cannot re-route bundles across topics: the
    AAD pins the topic, so the splice fails authentication."""
    content = org.store()
    org.delegate(content)
    broker = broker_for(harness, org, clock)
    broker_push(content, broker, org.sync_key, topic=CONTENT_TOPIC)

    bundles, _ = broker.fetch_bundles(CONTENT_TOPIC)
    assert bundles
    spliced = {k: bundles[0][k] for k in ("hashes", "size", "ciphertext")}
    spliced["v"] = 1
    # the same bytes open fine on their own topic...
    assert open_bundle(org.sync_key, ORG, CONTENT_TOPIC, spliced)
    # ...and fail authentication when re-routed to the authority topic
    with pytest.raises(BundleError, match="authentication"):
        open_bundle(org.sync_key, ORG, AUTHORITY_TOPIC, spliced)


def test_authority_scoped_cert_cannot_reach_content_topics(harness, org, clock):
    syncer = KeyPair.generate()
    cert = issue_cert(
        org.root,
        syncer.public_hex,
        scope=(f"topic:{AUTHORITY_TOPIC}",),
        org=ORG,
        subject=Subject("agent", "sync-1"),
        not_before=NOW - 50,
        not_after=NOW + 200 * DAY,
    )
    scoped = broker_for(harness, org, clock, key=syncer, cert=cert)
    replica = org.store()
    org.delegate(replica)
    # authority topic: full sync powers
    assert broker_push(replica, scoped, org.sync_key) == 2
    # content topic: every verb refused at the I4 gate
    from tools.network.ledger.broker import BrokerError

    with pytest.raises(BrokerError, match="403"):
        broker_push(replica, scoped, org.sync_key, topic=CONTENT_TOPIC)
    with pytest.raises(BrokerError, match="403"):
        scoped.poll_hints(CONTENT_TOPIC)
    with pytest.raises(BrokerError, match="403"):
        scoped.fetch_bundles(CONTENT_TOPIC)


def test_pull_refuses_wrong_key_material(harness, org, clock):
    """A non-member holding the topic endpoints but not the sync key gets
    ciphertext it can never open (the L6 guarantee, seen from outside):
    every bundle is quarantined and the pass fails loudly."""
    a = org.store()
    org.delegate(a)
    broker = broker_for(harness, org, clock)
    broker_push(a, broker, org.sync_key)

    outsider = org.store()
    with pytest.raises(BrokerError, match="undecryptable"):
        broker_pull(outsider, broker, b"\x37" * 32)
    assert len(outsider.ledger) == 1  # nothing ingested past genesis


def test_one_poisoned_bundle_does_not_brick_the_mailbox(harness, org, clock):
    """The broker is blind, so it stores any well-shaped ciphertext; a bad
    deposit (wrong key, truncated blob) must be quarantined, not allowed
    to wedge every honest replica's pull forever."""
    import base64

    broker = broker_for(harness, org, clock)
    # a buggy member deposits garbage claiming two fake event ids
    garbage = base64.b64encode(b"\x41" * 96).decode("ascii")
    broker._call(
        broker._topic_path(AUTHORITY_TOPIC, "bundles"),
        {"v": 1, "hashes": ["ab" * 32, "cd" * 32], "ciphertext": garbage},
    )
    # honest traffic lands after it
    live = org.store()
    org.delegate(live)
    broker_push(live, broker, org.sync_key)

    behind = org.store()
    pulled, cursor = broker_pull(behind, broker, org.sync_key)
    assert pulled == 1
    assert behind.fold().fingerprint() == live.fold().fingerprint()
    # and the poisoned row stays behind the cursor: the next pull is clean
    org.delegate(live)
    broker_push(live, broker, org.sync_key)
    pulled, _ = broker_pull(behind, broker, org.sync_key, cursor=cursor)
    assert pulled == 1


def test_deposited_but_unannounced_events_are_recovered_when_referenced(
    harness, org, clock
):
    """broker_push deposits before announcing; if the announce step dies,
    the events are invisible until re-announced — but a pull that NEEDS
    them (announced descendants) recovers them by hash."""
    live = org.store()
    first = org.delegate(live)
    broker = broker_for(harness, org, clock)

    # simulate a push that crashed after deposit, before publish_heads:
    from tools.network.ledger.bundles import seal_bundle

    bundle = seal_bundle(org.sync_key, ORG, AUTHORITY_TOPIC,
                         [org.genesis, live.get(first)])
    broker.deposit(AUTHORITY_TOPIC, bundle)

    behind = org.store()
    pulled, cursor = broker_pull(behind, broker, org.sync_key)
    assert pulled == 0  # nothing announced: notification plane is quiet

    # a later event by anyone announces heads that descend from the
    # orphaned deposit; the puller catches up
    org.delegate(live)
    broker_push(live, broker, org.sync_key)
    pulled, _ = broker_pull(behind, broker, org.sync_key, cursor=cursor)
    assert pulled == 2
    assert behind.fold().fingerprint() == live.fold().fingerprint()

    # a replica whose bundle cursor is already PAST the orphaned deposit
    # must chase the missing ancestry by hash instead of stalling
    skipper = org.store()
    pulled, _ = broker_pull(
        skipper, broker, org.sync_key, cursor=BrokerCursor(bundles=1)
    )
    assert pulled == 2
    assert skipper.fold().fingerprint() == live.fold().fingerprint()
