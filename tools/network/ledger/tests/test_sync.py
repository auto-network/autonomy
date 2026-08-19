"""F3 peer sync — heads exchange + fetch-missing-by-hash, and the sealed
bundle crypto the broker mailbox carries (spec §6–7; bead auto-rrzrt).

Acceptance pinned here:
- two replicas diverge offline → one direct-path pass → converged fold;
- offline catch-up: N rounds behind reconciles in ONE round trip;
- bundles: broker-visible face is hashes+size only, AAD binds org+topic
  (a content bundle can never replay into the authority topic), tamper
  and wrong-key all fail closed.
"""

from __future__ import annotations

import pytest

from tools.network.idkit import KeyPair
from tools.network.ledger import (
    sign_delegate_proof,
    HLC,
    BundleError,
    Event,
    LedgerStore,
    SyncError,
    SyncPeer,
    decode_message,
    derive_sync_key,
    encode_message,
    make_event,
    open_bundle,
    seal_bundle,
    sync_pair,
    validate_bundle,
)
from tools.network.ledger.bundles import _aad
from tools.network.ledger.sync import MAX_SYNC_HEADS
from tools.network.ledger.testkit import T0, OrgSim

ORG = "33333333-3333-4333-8333-333333333333"


@pytest.fixture
def org() -> OrgSim:
    return OrgSim(ORG)


def diverge(org: OrgSim, a: LedgerStore, b: LedgerStore, n_a: int = 3, n_b: int = 2):
    """Concurrent offline histories: n_a root events on a, n_b on b."""
    for _ in range(n_a):
        org.delegate(a, KeyPair.generate())
    for _ in range(n_b):
        org.delegate(b, KeyPair.generate())


# ── acceptance: diverge → one pass → converged ───────────────────────


def test_diverged_replicas_converge_in_one_pass(org):
    a, b = org.store(), org.store()
    diverge(org, a, b)
    assert a.fold().fingerprint() != b.fold().fingerprint()

    report = sync_pair(a, b)

    assert report.round_trips == 1
    # b ships its 2 fresh events plus the shared genesis (it does not know
    # a's heads, so it overshoots — idempotent ingest absorbs the overlap);
    # a answers b's wants with exactly its 3 fresh events.
    assert report.pulled == 3 and report.pushed == 3
    assert a.fold().fingerprint() == b.fold().fingerprint() == report.fingerprint
    assert a.ledger.all_ids() == b.ledger.all_ids()
    # fold judgement identical too (L1 over the synced set)
    assert a.fold().valid == b.fold().valid


def test_sync_is_idempotent(org):
    a, b = org.store(), org.store()
    diverge(org, a, b)
    sync_pair(a, b)
    again = sync_pair(a, b)
    assert again.pulled == 0 and again.pushed == 0


def test_offline_node_catches_up_in_one_round_trip(org):
    """N rounds of updates while offline → ONE pass, ONE round trip."""
    live, offline = org.store(), org.store()
    rounds = 10
    for _ in range(rounds):
        org.delegate(live, KeyPair.generate())  # a "round" of org activity

    report = sync_pair(offline, live)

    assert report.round_trips == 1
    assert report.pulled == rounds
    assert offline.fold().fingerprint() == live.fold().fingerprint()


def test_cold_bootstrap_from_trusted_peer(org):
    seeded = org.store()
    diverge(org, seeded, seeded, 2, 0)
    empty = LedgerStore()
    report = sync_pair(empty, seeded)
    assert report.round_trips == 1
    assert empty.fold().fingerprint() == seeded.fold().fingerprint()


# ── protocol edges ───────────────────────────────────────────────────


def test_different_orgs_never_merge():
    a = OrgSim("11111111-1111-4111-8111-111111111111").store()
    b = OrgSim("22222222-2222-4222-8222-222222222222").store()
    with pytest.raises(SyncError, match="different orgs"):
        sync_pair(a, b)


def test_expected_genesis_pins_a_fresh_replica(org):
    seeded = org.store()
    fresh = SyncPeer(LedgerStore(), expected_genesis="ab" * 32)
    resp = SyncPeer(seeded).respond(fresh.request())
    with pytest.raises(SyncError, match="expected org"):
        fresh.absorb(resp)


def test_null_genesis_declaration_cannot_bypass_the_pin(org):
    """A responder declaring genesis=null must not smuggle a foreign org
    into a pinned fresh replica — the events themselves are pinned."""
    foreign = org.store()
    fresh = SyncPeer(LedgerStore(), expected_genesis="ab" * 32)
    resp = SyncPeer(foreign).respond(fresh.request())
    resp["genesis"] = None  # forged declaration
    with pytest.raises(SyncError, match="foreign genesis"):
        fresh.absorb(resp)
    assert len(fresh.store.ledger) == 0  # nothing was ingested


def test_anchored_replica_rejects_foreign_genesis_in_pack(org):
    victim = org.store()
    foreign = OrgSim("22222222-2222-4222-8222-222222222222")
    push = {
        "t": "sync-push",
        "v": 1,
        "events": [foreign.genesis.to_json().decode("ascii")],
    }
    with pytest.raises(SyncError, match="foreign genesis"):
        SyncPeer(victim).receive_push(push)


def test_wide_dag_clamps_heads_and_still_converges(org):
    """A DAG wider than MAX_SYNC_HEADS declares a clamped subset instead
    of wedging the exchange; convergence still completes (overshoot)."""
    wide, behind = org.store(), org.store()
    gid = org.genesis.event_id
    for _ in range(MAX_SYNC_HEADS + 6):  # 70 concurrent branches
        org.delegate(wide, KeyPair.generate(), parents=[gid])
    assert len(wide.heads()) == MAX_SYNC_HEADS + 6

    req = SyncPeer(wide).request()
    assert len(req["heads"]) == MAX_SYNC_HEADS  # clamped, valid on the wire
    encode_message(req)

    report = sync_pair(behind, wide)
    assert report.round_trips == 1
    assert behind.fold().fingerprint() == wide.fold().fingerprint()


def test_non_ascii_wire_is_a_clean_protocol_error(org):
    a = org.store()
    push = {"t": "sync-push", "v": 1, "events": ['{"café": 1}']}
    with pytest.raises(SyncError, match="ASCII"):
        SyncPeer(a).receive_push(push)


def test_wire_roundtrip_and_validation(org):
    a = org.store()
    req = SyncPeer(a).request()
    assert decode_message(encode_message(req)) == req

    with pytest.raises(SyncError, match="unknown sync message type"):
        decode_message(b'{"t": "gossip", "v": 1}')
    with pytest.raises(SyncError, match="not valid JSON"):
        decode_message(b"nope")
    with pytest.raises(SyncError, match="exactly"):
        encode_message({"t": "sync-push", "v": 1, "events": [], "extra": 1})
    with pytest.raises(SyncError, match="lowercase hex"):
        encode_message({"t": "sync-req", "v": 1, "genesis": None, "heads": ["ZZ" * 32]})
    with pytest.raises(SyncError, match="sorted"):
        heads = sorted(a.heads())[::-1] * 2
        encode_message({"t": "sync-req", "v": 1, "genesis": None, "heads": heads})


def test_role_mismatch_messages_rejected(org):
    a, b = org.store(), org.store()
    peer_a, peer_b = SyncPeer(a), SyncPeer(b)
    req = peer_a.request()
    with pytest.raises(SyncError, match="expected sync-req"):
        peer_b.respond({"t": "sync-push", "v": 1, "events": []})
    resp = peer_b.respond(req)
    with pytest.raises(SyncError, match="expected sync-resp"):
        peer_a.absorb(req)
    assert peer_a.absorb(resp) is None  # identical replicas: nothing to push


# ── sealed bundles (L6 payloads) ─────────────────────────────────────


@pytest.fixture
def sealed(org):
    store = org.store()
    org.delegate(store, KeyPair.generate())
    key = derive_sync_key(org.genesis)
    return org, store, key, seal_bundle(key, ORG, "authority", store.events())


def test_bundle_roundtrip_and_broker_visible_face(sealed):
    org, store, key, bundle = sealed
    # the broker-visible face: version, hashes, size, opaque blob — no more
    assert set(bundle) == {"v", "hashes", "size", "ciphertext"}
    assert bundle["hashes"] == sorted(e.event_id for e in store.events())
    for event in store.events():
        assert event.to_json().decode("ascii") not in bundle["ciphertext"]

    events = open_bundle(key, ORG, "authority", bundle)
    assert sorted(e.event_id for e in events) == bundle["hashes"]
    assert all(isinstance(e, Event) for e in events)


def test_bundle_fails_closed(sealed):
    org, _store, key, bundle = sealed

    with pytest.raises(BundleError, match="authentication"):  # cross-topic splice
        open_bundle(key, ORG, "content.notes", bundle)
    with pytest.raises(BundleError, match="authentication"):  # cross-org splice
        open_bundle(key, "44444444-4444-4444-8444-444444444444", "authority", bundle)
    with pytest.raises(BundleError, match="authentication"):  # wrong key
        open_bundle(b"\x00" * 32, ORG, "authority", bundle)

    doctored = dict(bundle, hashes=sorted(bundle["hashes"] + ["ab" * 32]))
    with pytest.raises(BundleError, match="authentication"):  # doctored manifest
        open_bundle(key, ORG, "authority", doctored)

    import base64

    blob = bytearray(base64.b64decode(bundle["ciphertext"]))
    blob[-1] ^= 0x01
    tampered = dict(bundle, ciphertext=base64.b64encode(bytes(blob)).decode("ascii"))
    with pytest.raises(BundleError, match="authentication"):  # flipped bit
        open_bundle(key, ORG, "authority", tampered)


def test_bundle_manifest_must_match_decrypted_events(org):
    """Defense in depth: a manifest sealed over the wrong events fails the
    post-decrypt check even though its AAD authenticates."""
    import base64
    import os

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from tools.network.idkit import canonical_json

    store = org.store()
    key = derive_sync_key(org.genesis)
    lie = ["ab" * 32]  # manifest names an event the plaintext does not carry
    plaintext = canonical_json([org.genesis.to_json().decode("ascii")])
    nonce = os.urandom(12)
    blob = nonce + AESGCM(key).encrypt(nonce, plaintext, _aad(ORG, "authority", lie))
    forged = {
        "v": 1,
        "hashes": lie,
        "size": len(blob),
        "ciphertext": base64.b64encode(blob).decode("ascii"),
    }
    with pytest.raises(BundleError, match="manifest does not match"):
        open_bundle(key, ORG, "authority", forged)


def test_bundle_structural_validation(sealed):
    _org, _store, _key, bundle = sealed
    with pytest.raises(BundleError, match="exactly"):
        validate_bundle(dict(bundle, events=["smuggled plaintext"]))
    with pytest.raises(BundleError, match="sorted"):
        validate_bundle(dict(bundle, hashes=bundle["hashes"] * 2))
    with pytest.raises(BundleError, match="version"):
        validate_bundle(dict(bundle, v=2))


def test_sync_key_is_deterministic_and_org_specific(org):
    other = OrgSim("22222222-2222-4222-8222-222222222222")
    assert derive_sync_key(org.genesis) == derive_sync_key(org.genesis)
    assert derive_sync_key(org.genesis) != derive_sync_key(other.genesis)
    delegate_child = KeyPair.generate()
    delegate_event = make_event(
        org.root,
        {
            "type": "delegate",
            "child_pub": delegate_child.public_hex,
            "scope": ["link:publish"],
            "can_redelegate": False,
            "proof": sign_delegate_proof(
                delegate_child, org.genesis.event_id, org.root.public_hex,
                ["link:publish"],
            ),
        },
        [org.genesis.event_id],
        HLC(T0 + 1),
    )
    with pytest.raises(BundleError, match="genesis"):
        derive_sync_key(delegate_event)
