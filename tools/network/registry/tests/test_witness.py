"""F4 acceptance — the equivocation witness (spec §6 role 2, L5).

End-to-end over the real HTTP surface. The witness keeps a per-(org,
topic) append-only, hash-chained log of published head-sets and serves each
tip signed by the registry witness key. Pinned here:

- **Two-client fork detection** — a split-view registry shows A and B
  different head-sets; both verifiers flag it and hold a provable
  transcript (the two signed responses), independently re-checkable.
- **Append-only** — a superseding head-set advances the chain; a head-set
  that retracts a non-superseded head is rejected; the log itself only
  ever grows (seq monotonic, entries immutable, chained by prev).
- **Witnessed-head query (F7 seam)** — "state as of witnessed head H"
  folds deterministically, and only against a witness-signed head-set.
- **Blindness (L5)** — the witness's on-disk state is hashes, pubkeys, and
  timestamps only: no event wire, no plaintext marker.
- **Pin enforcement** — an attestation under a different key never counts;
  a split server cannot escape by signing each half under its own key.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tools.network.idkit import KeyPair, Subject, issue_cert
from tools.network.ledger.testkit import OrgSim
from tools.network.ledger.witness import (
    EquivocationProof,
    WitnessClient,
    WitnessEquivocation,
    WitnessError,
    WitnessJournal,
    WitnessRetraction,
    WitnessStale,
    dominates,
    witnessed_fold,
)
from tools.network.registry.app import create_app
from tools.network.registry.witness import (
    WitnessFormatError,
    build_entry,
    entry_id,
    sign_attestation,
    verify_attestation,
)

from .conftest import DAY, NOW, ORG, Clock, register

TOPIC = "authority"
CONTENT = "content.notes"


class Org(OrgSim):
    def __init__(self):
        super().__init__(ORG)


class Recorder:
    """TestClient transport (method, path, body) -> (status, json)."""

    def __init__(self, client: TestClient):
        self.client = client

    def __call__(self, method: str, path: str, body: dict):
        r = self.client.request(method, path, json=body)
        return r.status_code, r.json()


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
def app(clock, registry_db, org):
    app = create_app(registry_db, now_fn=clock)
    client = TestClient(app)
    assert register(client, clock, org.root).status_code == 201
    return app


@pytest.fixture
def transport(app):
    return Recorder(TestClient(app))


@pytest.fixture
def witness_pub(app):
    return app.state.witness_key.public_hex


def client_for(transport, org, clock, key=None, cert=None) -> WitnessClient:
    return WitnessClient(transport, ORG, key or org.root, cert=cert,
                         now_fn=lambda: clock.now)


# ── basics: publish, serve, pin ──────────────────────────────────────


def test_publish_serves_a_signed_tip(transport, org, clock, witness_pub):
    store = org.store()
    org.delegate(store)
    heads = list(store.heads())
    wc = client_for(transport, org, clock)

    att = wc.publish(TOPIC, heads)
    entry = verify_attestation(att, witness_pub)  # verifies under the pinned key
    assert entry["seq"] == 1 and entry["prev"] is None
    assert entry["heads"] == sorted(heads)
    assert entry["publisher"] == org.root.public_hex
    # the served head is byte-identical to what any member reads
    assert wc.head(TOPIC) == att


def test_republishing_the_same_headset_is_idempotent(transport, org, clock):
    store = org.store()
    org.delegate(store)
    heads = list(store.heads())
    wc = client_for(transport, org, clock)

    a1 = wc.publish(TOPIC, heads)
    clock.advance(30)
    a2 = wc.publish(TOPIC, heads)  # heartbeat, identical head-set
    assert a1 == a2  # log did not grow
    entries, _ = wc.since(TOPIC, 0)
    assert len(entries) == 1


def test_pin_rejects_a_foreign_witness_key(transport, org, clock, witness_pub):
    store = org.store()
    org.delegate(store)
    wc = client_for(transport, org, clock)
    att = wc.publish(TOPIC, list(store.heads()))
    # verifies under the real pinned key, not under an impostor's
    assert verify_attestation(att, witness_pub)
    impostor = KeyPair.generate().public_hex
    with pytest.raises(WitnessFormatError):
        verify_attestation(att, impostor)


# ── acceptance 1: two-client fork detection, provable transcript ─────


class SplitViewWitness:
    """A dishonest registry that shows two members two histories.

    Honest to A; to B it re-signs each ``witness/head`` response over a
    swapped head-set with the SAME registry witness key — precisely the
    forgery a T1 broker can mount, and precisely the one its own signature
    makes provable. Flip ``victim`` to choose whose view is served.
    """

    def __init__(self, app, fork_heads):
        self.inner = Recorder(TestClient(app))
        self.witness_key = app.state.witness_key
        self.fork_heads = sorted(fork_heads)
        self.victim = "A"

    def __call__(self, method, path, body):
        status, data = self.inner(method, path, body)
        if self.victim == "B" and path.endswith("/witness/head") and data.get("attestation"):
            e = data["attestation"]["entry"]
            forged = build_entry(e["org"], e["topic"], e["seq"], self.fork_heads,
                                 e["prev"], e["publisher"])
            data = {"topic": e["topic"],
                    "attestation": sign_attestation(self.witness_key, forged)}
        return status, data


def test_two_clients_shown_different_histories_both_prove_it(app, org, clock, witness_pub):
    # Alice's real frontier and Bob's forged one (a fork the server invents).
    store = org.store()
    org.delegate(store)
    alice_heads = list(store.heads())
    fork = org.store()
    org.delegate(fork)
    org.delegate(fork)
    bob_heads = list(fork.heads())
    assert alice_heads != bob_heads

    split = SplitViewWitness(app, bob_heads)
    wc = WitnessClient(split, ORG, org.root, now_fn=lambda: clock.now)
    wc.publish(TOPIC, alice_heads)  # one honest publish → seq 1 for everyone

    split.victim = "A"
    att_a = wc.head(TOPIC)
    split.victim = "B"
    att_b = wc.head(TOPIC)

    # each member journals the tip it was served; both at seq 1
    ja = WitnessJournal(witness_pub, org=ORG, topic=TOPIC)
    jb = WitnessJournal(witness_pub, org=ORG, topic=TOPIC)
    ja.admit(att_a, store)
    jb.admit(att_b, fork)
    assert att_a != att_b

    # they gossip their witnessed tips; each detects the fork
    proof_a = ja.reconcile(att_b)
    proof_b = jb.reconcile(att_a)
    assert proof_a is not None and proof_b is not None
    assert proof_a.kind == "split-seq"

    # the transcript is self-contained: verifiable by anyone, under the
    # pinned key, from the two signed responses alone
    assert proof_a.verify(witness_pub)
    assert proof_b.verify(witness_pub)
    transcript = proof_a.transcript()
    reconstructed = EquivocationProof(transcript["a"], transcript["b"], transcript["kind"])
    assert reconstructed.verify(witness_pub)
    # and it does NOT verify under the wrong key (the pin is load-bearing)
    assert not reconstructed.verify(KeyPair.generate().public_hex)


def test_a_forged_next_entry_that_breaks_the_chain_is_proven(app, org, clock, witness_pub):
    """A server that grows the chain from a DIFFERENT seq-N entry than the
    one a member holds signed produces a fork-prev proof."""
    store = org.store()
    org.delegate(store)
    wc = WitnessClient(Recorder(TestClient(app)), ORG, org.root, now_fn=lambda: clock.now)
    att1 = wc.publish(TOPIC, list(store.heads()))  # seq 1, held by the member

    org.delegate(store)
    heads2 = list(store.heads())
    # the server signs a seq-2 entry rooted on a fabricated seq-1 entry
    forged2 = build_entry(ORG, TOPIC, 2, heads2, prev="ff" * 32,
                          publisher=org.root.public_hex)
    att2 = sign_attestation(app.state.witness_key, forged2)

    journal = WitnessJournal(witness_pub, org=ORG, topic=TOPIC)
    journal.admit(att1, store)
    with pytest.raises(WitnessEquivocation) as exc:
        journal.admit(att2, store)
    assert exc.value.proof.kind == "fork-prev"
    assert exc.value.proof.verify(witness_pub)


def test_both_sides_of_a_chain_break_produce_a_proof(app, org, witness_pub):
    """A fork-prev split must be provable from EITHER side: the member
    holding the honest N shown the bad N+1, AND the member holding the bad
    N+1 shown the honest N. Both victims catch it — not just one."""
    wk = app.state.witness_key
    store = org.store()
    org.delegate(store)
    h1 = list(store.heads())
    org.delegate(store)
    h2 = list(store.heads())

    seq1 = sign_attestation(wk, build_entry(ORG, TOPIC, 1, h1, None, org.root.public_hex))
    # a bad N+1 rooted on a fabricated seq-1 entry (breaks the chain)
    seq2_bad = sign_attestation(
        wk, build_entry(ORG, TOPIC, 2, h2, "ff" * 32, org.root.public_hex)
    )

    # A holds the honest N, is shown the bad N+1 (the direction that worked)
    ja = WitnessJournal(witness_pub, org=ORG, topic=TOPIC)
    ja.admit(seq1, store)
    proof_a = ja.reconcile(seq2_bad)

    # B first-witnessed the bad N+1 (TOFU at seq 2), is later shown honest N
    jb = WitnessJournal(witness_pub, org=ORG, topic=TOPIC)
    jb.admit(seq2_bad, store)
    proof_b = jb.reconcile(seq1)

    assert proof_a is not None and proof_b is not None
    assert proof_a.kind == proof_b.kind == "fork-prev"
    assert proof_a.verify(witness_pub) and proof_b.verify(witness_pub)
    # both sides derive the SAME (lower, higher) ordered proof
    assert proof_a.transcript() == proof_b.transcript()


def test_honest_adjacent_chain_reconciles_clean_from_either_side(app, org, witness_pub):
    """The symmetric check must not false-positive on an honest chain: N and
    a well-linked N+1 reconcile to no proof, whichever one a member holds."""
    wk = app.state.witness_key
    store = org.store()
    org.delegate(store)
    h1 = list(store.heads())
    org.delegate(store)
    h2 = list(store.heads())

    seq1 = sign_attestation(wk, build_entry(ORG, TOPIC, 1, h1, None, org.root.public_hex))
    seq2 = sign_attestation(
        wk, build_entry(ORG, TOPIC, 2, h2, seq1["entry_id"], org.root.public_hex)
    )

    holds_n = WitnessJournal(witness_pub, org=ORG, topic=TOPIC)
    holds_n.admit(seq1, store)
    assert holds_n.reconcile(seq2) is None

    holds_n1 = WitnessJournal(witness_pub, org=ORG, topic=TOPIC)
    holds_n1.admit(seq2, store)
    assert holds_n1.reconcile(seq1) is None


# ── acceptance 2: append-only — supersede passes, retraction rejected ─


def test_superseding_headset_advances_the_chain(transport, org, clock, witness_pub):
    store = org.store()
    org.delegate(store)
    wc = client_for(transport, org, clock)
    wc.publish(TOPIC, list(store.heads()))  # seq 1

    org.delegate(store)  # a descendant head supersedes the old frontier
    wc.publish(TOPIC, list(store.heads()))  # seq 2

    journal = WitnessJournal(witness_pub, org=ORG, topic=TOPIC)
    entries, _ = wc.since(TOPIC, 0)
    advanced = journal.admit_chain(entries, store)
    assert advanced == 2
    assert journal.seq == 2


def test_retracting_headset_is_rejected(app, org, clock, witness_pub):
    """A chained-but-regressed head-set (drops a head no descendant
    supersedes) is a provable append-only violation at the frontier."""
    store = org.store()
    first = org.delegate(store)
    heads1 = list(store.heads())
    wc = WitnessClient(Recorder(TestClient(app)), ORG, org.root, now_fn=lambda: clock.now)
    att1 = wc.publish(TOPIC, heads1)  # seq 1 @ the current frontier

    # the server appends a well-chained seq-2 entry whose head-set points
    # BACK at genesis — an ancestor of heads1, superseding nothing
    genesis_id = org.genesis.event_id
    regressed = build_entry(ORG, TOPIC, 2, [genesis_id], prev=att1["entry_id"],
                            publisher=org.root.public_hex)
    att2 = sign_attestation(app.state.witness_key, regressed)

    journal = WitnessJournal(witness_pub, org=ORG, topic=TOPIC)
    journal.admit(att1, store)
    assert not dominates(store, [genesis_id], heads1)
    with pytest.raises(WitnessRetraction):
        journal.admit(att2, store)


def test_a_stale_tip_is_refused(transport, org, clock, witness_pub):
    store = org.store()
    org.delegate(store)
    wc = client_for(transport, org, clock)
    a1 = wc.publish(TOPIC, list(store.heads()))
    org.delegate(store)
    a2 = wc.publish(TOPIC, list(store.heads()))

    journal = WitnessJournal(witness_pub, org=ORG, topic=TOPIC)
    journal.admit(a1, store)
    journal.admit(a2, store)
    with pytest.raises(WitnessStale):
        journal.admit(a1, store)  # server tries to walk the tip backwards


def test_log_grows_append_only_and_stays_chained(transport, org, clock, witness_pub):
    store = org.store()
    ids = []
    wc = client_for(transport, org, clock)
    for _ in range(4):
        org.delegate(store)
        att = wc.publish(TOPIC, list(store.heads()))
        ids.append(att["entry_id"])
        clock.advance(10)

    entries, _ = wc.since(TOPIC, 0)
    assert [e["entry"]["seq"] for e in entries] == [1, 2, 3, 4]  # monotonic
    # each entry chains to its predecessor's content address; genesis prev is null
    assert entries[0]["entry"]["prev"] is None
    for prev, cur in zip(entries, entries[1:]):
        assert cur["entry"]["prev"] == prev["entry_id"]
        assert cur["entry"]["prev"] == entry_id(prev["entry"])
    # ids are stable content addresses (no rewrite mutated a past entry)
    assert [e["entry_id"] for e in entries] == ids


# ── acceptance 3: witnessed-head query (the F7 seam) ─────────────────


def test_witnessed_fold_is_deterministic_and_pinned(transport, org, clock, witness_pub):
    store = org.store()
    for _ in range(3):
        org.delegate(store)
    wc = client_for(transport, org, clock)
    att = wc.publish(TOPIC, list(store.heads()))

    folded = witnessed_fold(store, att, witness_pub)
    assert folded.fingerprint() == store.fold(heads=att["entry"]["heads"]).fingerprint()
    # a head-set that is not witness-signed is not an as-of point
    forged = dict(att, sig="00" * 64)
    with pytest.raises(WitnessError):
        witnessed_fold(store, forged, witness_pub)


# ── acceptance 4: blindness (L5) ─────────────────────────────────────


def test_witness_disk_holds_only_hashes(app, org, clock, registry_db):
    store = org.store()
    for _ in range(3):
        org.delegate(store)
    wc = WitnessClient(Recorder(TestClient(app)), ORG, org.root, now_fn=lambda: clock.now)
    for _ in range(3):
        org.delegate(store)
        wc.publish(TOPIC, list(store.heads()))
        clock.advance(5)

    app.state.store._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    disk = b""
    for suffix in ("", "-wal", "-shm"):
        try:
            with open(registry_db + suffix, "rb") as fh:
                disk += fh.read()
        except FileNotFoundError:
            pass
    assert disk

    for event in store.events():
        assert event.to_json() not in disk, "event wire reached the witness store"
        assert event.sig.encode() not in disk, "event signature reached the witness"
        if event.type == "delegate":
            assert event.payload["child_pub"].encode() not in disk
    # head hashes ARE the allowance — they must be present
    for h in store.heads():
        assert h.encode() in disk
    for marker in (b'"payload"', b'"author_key"', b'"parents"'):
        assert marker not in disk


# ── scope gate: witness is Tier B, per-topic ─────────────────────────


def test_authority_scoped_cert_cannot_witness_a_content_topic(transport, org, clock):
    syncer = KeyPair.generate()
    cert = issue_cert(
        org.root, syncer.public_hex, scope=(f"topic:{TOPIC}",), org=ORG,
        subject=Subject("agent", "sync-1"), not_before=NOW - 50,
        not_after=NOW + 200 * DAY,
    )
    store = org.store()
    org.delegate(store)
    scoped = client_for(transport, org, clock, key=syncer, cert=cert)
    heads = list(store.heads())
    assert scoped.publish(TOPIC, heads)["entry"]["seq"] == 1
    with pytest.raises(WitnessError, match="403"):
        scoped.publish(CONTENT, heads)
    with pytest.raises(WitnessError, match="403"):
        scoped.head(CONTENT)


def test_witness_pubkey_is_discoverable(transport, app):
    status, body = transport("GET", "/v1/witness/pubkey", None)
    assert status == 200
    assert body["witness_pub"] == app.state.witness_key.public_hex
