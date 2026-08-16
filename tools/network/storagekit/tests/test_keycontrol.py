"""KeyControlStore — descriptor, credential and ParentBridge persistence.

The production store (``keycontrol.py``) that ``1e005d5c-c11`` §1b names,
exercised through the REAL acceptance layer and the REAL authority fold
via the conftest ``World``. Covers round-trip, content-addressed dedupe,
content-address tamper refusal, acceptance-failure propagation, the
storage-DAG ``ancestry`` closure (including its fail-closed refusal of an
unseen identifier), disk reopen, and co-location beside the ledger; then
bridge persistence, the ``(child, parent)`` edge index, recomputed
``history_complete``, store-backed ancestor recovery, and the pending
(dependency-deferral) store.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sqlite3
import threading

import pytest

from tools.network.idkit import KeyPair, canonical_json
from tools.network.ledger import LedgerStore, org_ledger_db_path
from tools.network.storagekit import (
    CommitmentError,
    MalformedRecordError,
    RecordSignatureError,
    acceptance,
    select_current_credential,
    state as state_mod,
)
from tools.network.storagekit import bridge as bridge_mod
from tools.network.storagekit import keycontrol
from tools.network.storagekit.acceptance import (
    AuthorityError,
    FrontierRecencyError,
    LossCoverageError,
    ScopeError,
)
from tools.network.storagekit.bridge import BridgeError
from tools.network.storagekit.credentials import build as build_credential
from tools.network.storagekit.keycontrol import (
    MAX_PENDING_BYTES,
    RECORD_TYPE_BRIDGE,
    Admission,
    BridgeContextError,
    KeyControlStore,
    PendingLimits,
    StoreCounters,
    StoredState,
    TamperError,
    UnknownStateError,
    UnsignedEdgeError,
)
from tools.network.storagekit.records import record_id

from .conftest import HLC0, World

# Distinct 32-byte KEM seeds — a credential's kem_key_id is a function of its
# seed (via the derived kem_public_key), so different seeds give different ids.
SEED_A = bytes(range(0, 32))
SEED_B = bytes(range(1, 33))
SEED_C = bytes(range(2, 34))


@pytest.fixture(autouse=True)
def orgs_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path))
    return tmp_path


def _minted(world: World):
    """A validly minted, acceptance-passing initial-state descriptor."""
    descriptor, _ = world.mint_initial_state(world.member(0))
    return descriptor


def _row_count(store: KeyControlStore) -> int:
    return store.db.execute("SELECT COUNT(*) FROM keycontrol_state").fetchone()[0]


# -- round-trip + dedupe ----------------------------------------------------------------


def test_descriptor_round_trips(world):
    descriptor = _minted(world)
    with KeyControlStore(":memory:") as store:
        store.accept_state(descriptor, world.fold, world.ancestry)
        got = store.get(descriptor.state_id)
        assert got is not None
        assert got.to_json() == descriptor.to_json()  # byte-identical


def test_restore_is_idempotent_no_duplicate_row(world):
    descriptor = _minted(world)
    with KeyControlStore(":memory:") as store:
        store.accept_state(descriptor, world.fold, world.ancestry)
        store.accept_state(descriptor, world.fold, world.ancestry)  # identical again
        assert _row_count(store) == 1
        assert len(store) == 1


def test_content_address_mismatch_is_refused(world):
    """A stored ``state_id`` that does not equal ``record_id`` over the row's
    own signed bytes is refused at open."""
    path = org_ledger_db_path("acme")
    descriptor = _minted(world)
    store = KeyControlStore(path)
    store.accept_state(descriptor, world.fold, world.ancestry)
    store.close()

    raw = sqlite3.connect(path)
    with raw:  # re-key the (unchanged, valid) wire under a wrong content address
        raw.execute("UPDATE keycontrol_state SET state_id = ?", ("ff" * 32,))
    raw.close()

    with pytest.raises(TamperError):
        KeyControlStore(path)


# -- acceptance refusals ----------------------------------------------------------------


def test_non_member_creator_refused_with_scope_error(world):
    """Real end-to-end refusal: a descriptor signed by a non-member creator
    is rejected by ``accept_state`` with the check-naming ``ScopeError`` and
    nothing is persisted."""
    outsider = KeyPair.generate()  # never admitted to the org
    fold = world.fold()
    descriptor, _ = state_mod.generate(
        outsider,
        world.gen,
        world.dom,
        (),
        sorted(fold.heads),
        sorted(fold.loss_heads),
        acceptance.loss_projection_digest(fold),
    )
    with KeyControlStore(":memory:") as store:
        with pytest.raises(ScopeError):
            store.accept_state(descriptor, world.fold, world.ancestry)
        assert store.get(descriptor.state_id) is None
        assert _row_count(store) == 0


@pytest.mark.parametrize(
    "error_cls",
    [ScopeError, AuthorityError, LossCoverageError, FrontierRecencyError],
)
def test_acceptance_error_propagates_and_persists_nothing(world, monkeypatch, error_cls):
    """Each distinct acceptance refusal class propagates unchanged — the
    store never rewraps it into a generic error — and no row is written on
    refusal."""
    descriptor = _minted(world)
    with KeyControlStore(":memory:") as store:

        def refuse(*args, **kwargs):
            raise error_cls(f"{error_cls.__name__} check failed")

        monkeypatch.setattr(acceptance, "accept_state", refuse)
        with pytest.raises(error_cls):
            store.accept_state(descriptor, world.fold, world.ancestry)
        assert store.get(descriptor.state_id) is None
        assert _row_count(store) == 0


# -- ancestry traversal -----------------------------------------------------------------


def _union_world():
    """A union state ``su`` over two concurrent advances ``sa``/``sb`` of a
    shared root ``s0`` — the storage-DAG domination fixture."""
    world = World(member_count=4)
    m0, m1 = world.member(0), world.member(1)
    s0, _ = world.mint_initial_state(m0)
    world.grant(m0, m1, s0)
    base = world.frontier()
    r2 = world.remove(world.member(2), parents=base)
    r3 = world.remove(world.member(3), parents=base)
    sa, _ = world.advance(m0, s0, heads=[r2])
    sb, _ = world.advance(m1, s0, heads=[r3])
    world.grant(m1, m0, sb)
    su, _ = world.union(m0, [sa, sb])
    return world, s0, sa, sb, su


def test_ancestry_two_parents_includes_both_and_self():
    world, s0, sa, sb, su = _union_world()
    with KeyControlStore(":memory:") as store:
        for descriptor in (s0, sa, sb, su):
            store.accept_state(descriptor, world.fold, world.ancestry)

        closure = store.ancestry([su.state_id])
        assert {su.state_id, sa.state_id, sb.state_id, s0.state_id} <= closure

        # Neither branch dominates the other.
        assert sb.state_id not in store.ancestry([sa.state_id])
        assert sa.state_id not in store.ancestry([sb.state_id])


def test_ancestry_unknown_identifier_raises_and_is_not_returned():
    world, s0, sa, sb, su = _union_world()
    with KeyControlStore(":memory:") as store:
        store.accept_state(s0, world.fold, world.ancestry)
        unseen = "ab" * 32
        with pytest.raises(UnknownStateError):
            store.ancestry([unseen])
        # Mixed batch: one seen, one unseen — still refused, closed.
        with pytest.raises(UnknownStateError):
            store.ancestry([s0.state_id, unseen])


# -- durability + co-location -----------------------------------------------------------


def test_store_reopens_from_disk():
    world, s0, sa, sb, su = _union_world()
    path = org_ledger_db_path("acme")
    store = KeyControlStore(path)
    for descriptor in (s0, sa, sb, su):
        store.accept_state(descriptor, world.fold, world.ancestry)
    store.close()

    with KeyControlStore(path) as reopened:
        for descriptor in (s0, sa, sb, su):
            got = reopened.get(descriptor.state_id)
            assert got is not None and got.to_json() == descriptor.to_json()
        # The storage-DAG survives the round-trip through disk.
        assert {sa.state_id, sb.state_id, s0.state_id} <= reopened.ancestry(
            [su.state_id]
        )


def test_slug_targets_the_org_own_db(world, orgs_dir):
    """Storing by slug writes into the org's own database and creates no
    database of its own.

    The negative is asserted as "no file appeared that the org database did
    not already account for", not as "no file named ``keycontrol.db``" --
    naming one hypothetical filename passes for every other name somebody
    might reach for instead.
    """
    descriptor = _minted(world)
    before = {p.name for p in orgs_dir.iterdir()}
    with KeyControlStore(slug="acme") as store:
        assert store.path == str(orgs_dir / "acme.db")
        store.accept_state(descriptor, world.fold, world.ancestry)
    new = {p.name for p in orgs_dir.iterdir()} - before
    # WAL/SHM sidecars belong to acme.db itself, not to a second store.
    assert new <= {"acme.db", "acme.db-wal", "acme.db-shm"}, (
        f"storing by slug created its own database: {sorted(new)}"
    )


def test_coexists_with_ledger_in_one_org_db(world):
    """Ledger tables and key-control tables share the org's own database
    file — the co-location the bead requires."""
    descriptor = _minted(world)
    path = org_ledger_db_path("acme")

    ledger = LedgerStore(path)
    ledger.append_bundle(world.sim.ledger.events())
    ledger.close()

    with KeyControlStore(path) as store:
        store.accept_state(descriptor, world.fold, world.ancestry)
        tables = {
            r[0]
            for r in store.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"keycontrol_state", "ledger_events", "ledger_heads"} <= tables

    # Both stores reopen cleanly from the shared file.
    with LedgerStore(path) as again:
        assert again.ledger.genesis_id == world.sim.genesis_id
    with KeyControlStore(path) as again:
        assert again.get(descriptor.state_id) is not None


# -- PersonaKemCredential persistence ---------------------------------------------------


def _cred_row_count(store: KeyControlStore) -> int:
    return store.db.execute(
        "SELECT COUNT(*) FROM keycontrol_credential"
    ).fetchone()[0]


def _credential(world: World, signer: KeyPair, seed: bytes, heads=None):
    """A validly built credential for *signer* in the world's org."""
    credential, _ = build_credential(
        signer, world.gen, seed, list(heads) if heads is not None else [world.gen], HLC0
    )
    return credential


def test_credential_round_trips(world):
    credential = _credential(world, world.member(0), SEED_A)
    with KeyControlStore(":memory:") as store:
        returned = store.accept_credential(credential)
        assert returned == credential
        got = store.get_credential(credential.kem_key_id)
        assert got is not None
        assert got.to_json() == credential.to_json()  # byte-identical


def test_credential_round_trips_through_disk(world):
    """Reopening from disk re-verifies each credential's content address —
    it is checked on hydrate, not only at write."""
    credential = _credential(world, world.member(0), SEED_A)
    path = org_ledger_db_path("acme")
    store = KeyControlStore(path)
    store.accept_credential(credential)
    store.close()

    with KeyControlStore(path) as reopened:
        got = reopened.get_credential(credential.kem_key_id)
        assert got is not None and got.to_json() == credential.to_json()


def test_restore_credential_is_idempotent_no_duplicate_row(world):
    credential = _credential(world, world.member(0), SEED_A)
    with KeyControlStore(":memory:") as store:
        store.accept_credential(credential)
        store.accept_credential(credential)  # identical again
        assert _cred_row_count(store) == 1
        assert store.credentials_for_persona(world.member(0).public_hex) == [credential]


def test_bad_signature_refused_terminally_and_persists_nothing(world):
    """A credential whose signature does not verify is refused terminally and
    leaves no row."""
    credential = _credential(world, world.member(0), SEED_A)
    forged = dataclasses.replace(
        credential, signature=KeyPair.generate().sign_hex(credential.signing_input())
    )
    with KeyControlStore(":memory:") as store:
        with pytest.raises(RecordSignatureError):
            store.accept_credential(forged)
        assert store.get_credential(credential.kem_key_id) is None
        assert _cred_row_count(store) == 0


def test_kem_key_id_not_matching_binding_refused(world):
    """A kem_key_id that does not equal ``compute_kem_key_id(binding)`` is
    refused terminally and leaves no row."""
    credential = _credential(world, world.member(0), SEED_A)
    flipped = ("1" if credential.kem_key_id[0] == "0" else "0") + credential.kem_key_id[1:]
    tampered = dataclasses.replace(credential, kem_key_id=flipped)
    with KeyControlStore(":memory:") as store:
        with pytest.raises(MalformedRecordError):
            store.accept_credential(tampered)
        assert _cred_row_count(store) == 0


def test_wrong_field_set_refused(world):
    """Fields that are not exactly ``_FIELDS`` are refused terminally."""
    credential = _credential(world, world.member(0), SEED_A)
    with KeyControlStore(":memory:") as store:
        with pytest.raises(MalformedRecordError):
            store.accept_credential({**credential.to_dict(), "extra": 1})
        assert _cred_row_count(store) == 0


def test_two_credentials_same_persona_both_persist(world):
    """Currency is not uniqueness in storage: two different credentials for
    one persona both persist and are both retrievable."""
    member = world.member(0)
    a = _credential(world, member, SEED_A)
    b = _credential(world, member, SEED_B)
    assert a.kem_key_id != b.kem_key_id
    with KeyControlStore(":memory:") as store:
        store.accept_credential(a)
        store.accept_credential(b)
        assert _cred_row_count(store) == 2
        assert store.get_credential(a.kem_key_id) == a
        assert store.get_credential(b.kem_key_id) == b
        by_persona = store.credentials_for_persona(member.public_hex)
        assert {c.kem_key_id for c in by_persona} == {a.kem_key_id, b.kem_key_id}


def test_retrieval_by_persona_is_scoped(world):
    """Retrieval by kem_key_id returns the one credential; retrieval by
    persona returns every credential for that persona and no other's."""
    m0, m1 = world.member(0), world.member(1)
    a = _credential(world, m0, SEED_A)
    b = _credential(world, m0, SEED_B)
    c = _credential(world, m1, SEED_C)
    with KeyControlStore(":memory:") as store:
        for credential in (a, b, c):
            store.accept_credential(credential)
        assert {x.kem_key_id for x in store.credentials_for_persona(m0.public_hex)} == {
            a.kem_key_id,
            b.kem_key_id,
        }
        assert {x.kem_key_id for x in store.credentials_for_persona(m1.public_hex)} == {
            c.kem_key_id
        }
        assert store.get_credential(c.kem_key_id) == c
        assert store.credentials_for_persona(KeyPair.generate().public_hex) == []


def test_admission_does_not_consult_a_fold(world):
    """A credential whose persona is absent from any fold — never admitted to
    the org — still stores and retrieves. Admission validates; it does not
    check membership."""
    outsider = KeyPair.generate()
    credential = _credential(world, outsider, SEED_A)
    with KeyControlStore(":memory:") as store:
        store.accept_credential(credential)
        assert store.get_credential(credential.kem_key_id) == credential
        assert store.credentials_for_persona(outsider.public_hex) == [credential]


def test_credential_content_address_mismatch_refused_at_open(world):
    """A stored ``kem_key_id`` that does not match the credential's binding
    over the row's own signed bytes is refused at open."""
    path = org_ledger_db_path("acme")
    credential = _credential(world, world.member(0), SEED_A)
    store = KeyControlStore(path)
    store.accept_credential(credential)
    store.close()

    raw = sqlite3.connect(path)
    with raw:  # re-key the (unchanged, valid) wire under a wrong content address
        raw.execute("UPDATE keycontrol_credential SET kem_key_id = ?", ("ff" * 32,))
    raw.close()

    with pytest.raises(TamperError):
        KeyControlStore(path)


def test_stored_set_feeds_select_current_credential_equal_frontiers(world):
    """A stored candidate set resolves under ``select_current_credential``
    with the CALLER's ledger ancestry — equal frontiers resolve by ascending
    kem_key_id."""
    member = world.member(0)
    heads = list(world.sim.ledger.heads())
    a = _credential(world, member, SEED_A, heads=heads)
    b = _credential(world, member, SEED_B, heads=heads)
    with KeyControlStore(":memory:") as store:
        store.accept_credential(a)
        store.accept_credential(b)
        candidates = store.credentials_for_persona(member.public_hex)
        # world.ancestry is the LEDGER's DAG (Ledger.ancestry), NOT store.ancestry.
        current = select_current_credential(candidates, world.ancestry)
        assert current == max([a, b], key=lambda c: c.kem_key_id)


def test_stored_set_feeds_select_current_credential_concurrent(world):
    """Incomparable frontiers over two concurrent authority events resolve by
    ascending kem_key_id — driven from the STORED set, unchanged."""
    member = world.member(0)
    base = list(world.sim.ledger.heads())
    x = world.sim.delegate(
        world.sim.root, KeyPair.generate(), ["link:publish"], parents=base
    )
    y = world.sim.delegate(
        world.sim.root, KeyPair.generate(), ["link:revoke"], parents=base
    )
    a = _credential(world, member, SEED_A, heads=[x])
    b = _credential(world, member, SEED_B, heads=[y])
    with KeyControlStore(":memory:") as store:
        store.accept_credential(a)
        store.accept_credential(b)
        candidates = store.credentials_for_persona(member.public_hex)
        current = select_current_credential(candidates, world.ancestry)
        assert current == max([a, b], key=lambda c: c.kem_key_id)


# ==== ParentBridge persistence =========================================================
#
# A bridge carries a PARENT state's secret encrypted under a key derived from
# the CHILD's secret (`bb971a32-ed1` §7). The cryptography lives in
# ``bridge.py`` and is reused unchanged; these tests exercise PERSISTENCE,
# INDEXING and the store-backed readings built on them.


def _bridges_for(world: World, descriptor) -> tuple:
    """The bridges the real lifecycle minted for *descriptor*'s parent edges."""
    return tuple(
        b for b in world.stores.kc.bridges if b.child_state_id == descriptor.state_id
    )


def _offer(store: KeyControlStore, bridge, **kw) -> Admission:
    """Offer a bridge the way transport does: raw wire plus a claimed id."""
    wire = bridge.to_json()
    return store.accept_bridge(record_id(wire), wire, **kw)


def _bridge_row_count(store: KeyControlStore) -> int:
    return store.db.execute("SELECT COUNT(*) FROM keycontrol_bridge").fetchone()[0]


def _stray_bridge(child_id=None, parent_id=None):
    """A validly signed bridge for states no store has seen — it can only
    ever DEFER, which is what the pending tests need."""
    return bridge_mod.create(
        KeyPair.generate(),
        genesis_id=os.urandom(32).hex(),
        domain_id=os.urandom(32).hex(),
        child_state_id=child_id or os.urandom(32).hex(),
        parent_state_id=parent_id or os.urandom(32).hex(),
        child_state_secret=os.urandom(32),
        parent_state_secret=os.urandom(32),
        authority_heads=[os.urandom(32).hex()],
    )


def _loaded_union_store(path=":memory:", **kw):
    """``s0 <- sa, sb <- su`` fully persisted, every bridge with it."""
    world, s0, sa, sb, su = _union_world()
    store = KeyControlStore(path, **kw)
    for descriptor in (s0, sa, sb, su):
        store.accept_state(
            descriptor,
            world.fold,
            world.ancestry,
            bridges=_bridges_for(world, descriptor),
        )
    return world, store, (s0, sa, sb, su)


# -- round-trip, dedupe, tamper ---------------------------------------------------------


def test_bridge_submitted_with_its_descriptor_round_trips():
    world, store, (s0, sa, sb, su) = _loaded_union_store()
    with store:
        offered = _bridges_for(world, su)
        assert len(offered) == 2  # a union bridges every parent
        for bridge in offered:
            got = store.get_bridge(bridge.bridge_id)
            assert got is not None
            assert got.to_json() == bridge.to_json()  # byte-identical


def test_accept_state_reports_the_bridges_it_persisted():
    world, s0, sa, sb, su = _union_world()
    with KeyControlStore(":memory:") as store:
        for descriptor in (s0, sa, sb):
            store.accept_state(
                descriptor, world.fold, world.ancestry,
                bridges=_bridges_for(world, descriptor),
            )
        offered = _bridges_for(world, su)
        result = store.accept_state(
            su, world.fold, world.ancestry, bridges=offered
        )
        assert set(result.bridges) == {b.bridge_id for b in offered}


def test_restore_bridge_is_idempotent_no_duplicate_row():
    world, store, (s0, sa, sb, su) = _loaded_union_store()
    with store:
        before = _bridge_row_count(store)
        for bridge in _bridges_for(world, su):
            assert _offer(store, bridge) is Admission.ACCEPTED  # identical again
        assert _bridge_row_count(store) == before


def test_bridge_content_address_mismatch_is_refused_at_open():
    """A stored ``bridge_id`` that does not equal SHA-256 of the row's own
    bytes is refused at open, exactly as descriptors are."""
    path = org_ledger_db_path("acme")
    _world, store, _states = _loaded_union_store(path)
    store.close()

    raw = sqlite3.connect(path)
    with raw:  # re-key one (unchanged, valid) wire under a wrong content address
        raw.execute(
            "UPDATE keycontrol_bridge SET bridge_id = ? WHERE bridge_id = "
            "(SELECT MIN(bridge_id) FROM keycontrol_bridge)",
            ("ff" * 32,),
        )
    raw.close()

    with pytest.raises(TamperError):
        KeyControlStore(path)


# -- accept_bridge: terminal refusals of untrusted wire ----------------------------------


def test_accept_bridge_refuses_non_canonical_wire():
    bridge = _stray_bridge()
    wire = bridge.to_json() + b" "  # one accepted byte form only
    with KeyControlStore(":memory:") as store:
        with pytest.raises(MalformedRecordError):
            store.accept_bridge(record_id(wire), wire)
        assert _bridge_row_count(store) == 0
        assert store.pending_count() == 0


def test_accept_bridge_refuses_a_wrong_content_address():
    bridge = _stray_bridge()
    wire = bridge.to_json()
    with KeyControlStore(":memory:") as store:
        with pytest.raises(TamperError):
            store.accept_bridge("ff" * 32, wire)
        assert _bridge_row_count(store) == 0
        assert store.pending_count() == 0


def test_accept_bridge_refuses_a_bad_signature():
    bridge = _stray_bridge()
    forged = dataclasses.replace(
        bridge, signature=KeyPair.generate().sign_hex(bridge.signing_input())
    )
    wire = forged.to_json()
    with KeyControlStore(":memory:") as store:
        with pytest.raises(RecordSignatureError):
            store.accept_bridge(record_id(wire), wire)
        assert _bridge_row_count(store) == 0
        assert store.pending_count() == 0


# -- arrival: dependency deferral --------------------------------------------------------


def test_bridge_ahead_of_its_states_is_deferred_then_admitted():
    """A bridge naming a state the store has not seen is DEFERRED and
    retried, never refused — the shipped design, not a hazard. It is
    admitted once its states arrive, with NO resubmission by the sender."""
    world, s0, sa, sb, su = _union_world()
    su_bridges = _bridges_for(world, su)
    with KeyControlStore(":memory:") as store:
        for bridge in su_bridges:
            assert _offer(store, bridge) is Admission.DEFERRED
            assert store.get_bridge(bridge.bridge_id) is None
        assert store.pending_count() == 2

        # The states arrive. Nothing is re-offered.
        for descriptor in (s0, sa, sb):
            store.accept_state(descriptor, world.fold, world.ancestry)
        assert store.pending_count() == 2  # still waiting on the child
        store.accept_state(su, world.fold, world.ancestry)

        for bridge in su_bridges:
            assert store.get_bridge(bridge.bridge_id) is not None
        assert store.pending_count() == 0
        assert store.history_complete(su.state_id) is True


def test_bridge_whose_parent_state_is_unseen_defers_on_the_parent():
    world, s0, sa, sb, su = _union_world()
    (su_sa,) = [b for b in _bridges_for(world, su) if b.parent_state_id == sa.state_id]
    with KeyControlStore(":memory:") as store:
        store.accept_state(s0, world.fold, world.ancestry)
        store.accept_state(su, world.fold, world.ancestry)  # child held, parent not
        assert _offer(store, su_sa) is Admission.DEFERRED
        [info] = store.pending_records()
        assert info.unmet_dependency_id == sa.state_id

        store.accept_state(sa, world.fold, world.ancestry)
        assert store.get_bridge(su_sa.bridge_id) is not None
        assert store.pending_count() == 0


# -- accept_bridge: edge refusals against the held descriptor ----------------------------


def _resigned_bridge(world, child, parent, *, issuer=None, **overrides):
    """A structurally valid, correctly signed bridge for the (child, parent)
    edge, with one context field overridden."""
    creator = issuer or world.member(0)
    m0 = world.member(0)
    return bridge_mod.create(
        creator,
        genesis_id=overrides.get("genesis_id", child.genesis_id),
        domain_id=overrides.get("domain_id", child.domain_id),
        child_state_id=child.state_id,
        parent_state_id=parent.state_id,
        child_state_secret=world.held(m0)[child.state_id],
        parent_state_secret=world.held(m0)[parent.state_id],
        authority_heads=overrides.get("authority_heads", child.authority_heads),
    )


def test_bridge_naming_an_unsigned_edge_is_refused():
    """``recover_ancestors`` groups by ``child_state_id`` alone and never
    consults the signed edge, so a stored ``su -> s0`` would be FOLLOWED even
    though ``su``'s signed parents are ``[sa, sb]``."""
    world, store, (s0, sa, sb, su) = _loaded_union_store()
    with store:
        assert s0.state_id not in su.parent_state_ids
        rogue = _resigned_bridge(world, su, s0)
        with pytest.raises(UnsignedEdgeError):
            _offer(store, rogue)
        assert store.get_bridge(rogue.bridge_id) is None
        assert store.pending_count() == 0


def test_bridge_disagreeing_on_issuer_persona_is_refused():
    world, store, (s0, sa, sb, su) = _loaded_union_store()
    with store:
        other = world.member(1)
        assert other.public_hex != su.creator_persona
        with pytest.raises(BridgeContextError):
            _offer(store, _resigned_bridge(world, su, sa, issuer=other))


def test_bridge_disagreeing_on_authority_heads_is_refused():
    world, store, (s0, sa, sb, su) = _loaded_union_store()
    with store:
        heads = sorted(set(su.authority_heads) | {world.gen})
        assert tuple(heads) != tuple(su.authority_heads)
        with pytest.raises(BridgeContextError):
            _offer(store, _resigned_bridge(world, su, sa, authority_heads=heads))


def test_bridge_disagreeing_on_genesis_id_is_refused():
    world, store, (s0, sa, sb, su) = _loaded_union_store()
    with store:
        with pytest.raises(BridgeContextError):
            _offer(store, _resigned_bridge(world, su, sa, genesis_id="aa" * 32))


def test_bridge_disagreeing_on_domain_id_is_refused():
    world, store, (s0, sa, sb, su) = _loaded_union_store()
    with store:
        with pytest.raises(BridgeContextError):
            _offer(store, _resigned_bridge(world, su, sa, domain_id="bb" * 32))


# -- history_complete: recomputed, tracking LOCAL AVAILABILITY ---------------------------


def test_history_complete_tracks_local_availability_not_admission():
    """Same descriptor, in order: both bodies present -> True; one pruned
    (§19) -> False; that body re-fetched BY PAIR -> True again. Recomputed
    from the store each time; no column stores it."""
    world, store, (s0, sa, sb, su) = _loaded_union_store()
    with store:
        # What a peer would serve on a §19 re-fetch, keyed the way §19 asks.
        remote = {
            (b.child_state_id, b.parent_state_id): b.to_json()
            for b in _bridges_for(world, su)
        }
        assert store.history_complete(su.state_id) is True

        (su_sa,) = [
            b for b in _bridges_for(world, su) if b.parent_state_id == sa.state_id
        ]
        assert store.prune_bridge_body(su_sa.bridge_id) is True
        assert store.history_complete(su.state_id) is False

        locator = store.bridge_locator(su.state_id, sa.state_id)
        assert locator is not None  # the edge resolves with no body here
        assert locator.body_present == frozenset()
        assert su_sa.bridge_id in locator.bridge_ids  # the audit anchor survives

        wire = remote[(locator.child_state_id, locator.parent_state_id)]
        assert store.accept_bridge(record_id(wire), wire) is Admission.ACCEPTED
        assert store.history_complete(su.state_id) is True


def _database_snapshot(store: KeyControlStore) -> dict:
    """Every row of every key-control table. Compared rather than named, so
    this notices a verdict recorded anywhere — including in a column nobody
    thought to guess."""
    tables = sorted(
        name
        for (name,) in store.db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
            " AND name LIKE 'keycontrol%'"
        )
    )
    return {t: sorted(map(repr, store.db.execute(f"SELECT * FROM {t}"))) for t in tables}


def test_history_complete_is_recomputed_and_records_nothing():
    """No column stores it: reading it writes nothing at all, and the reading
    after a reopen is derived from what is on disk, not from a cached verdict."""
    path = org_ledger_db_path("acme")
    world, store, (s0, sa, sb, su) = _loaded_union_store(path)
    (su_sa,) = [b for b in _bridges_for(world, su) if b.parent_state_id == sa.state_id]

    before = _database_snapshot(store)
    assert store.history_complete(su.state_id) is True
    assert store.history_complete(su.state_id) is True
    assert _database_snapshot(store) == before  # the reading recorded nothing

    store.prune_bridge_body(su_sa.bridge_id)
    assert store.history_complete(su.state_id) is False
    store.close()

    with KeyControlStore(path) as reopened:
        # A store that had read True before the prune reads False now.
        assert reopened.history_complete(su.state_id) is False


def test_descriptor_admits_with_fewer_bridges_than_parents_and_completes_later():
    """Property 6: a missing bridge reads history-incomplete WITHOUT raising,
    and completes when the missing bridges arrive."""
    world, s0, sa, sb, su = _union_world()
    offered = _bridges_for(world, su)
    with KeyControlStore(":memory:") as store:
        for descriptor in (s0, sa, sb):
            store.accept_state(
                descriptor, world.fold, world.ancestry,
                bridges=_bridges_for(world, descriptor),
            )
        result = store.accept_state(su, world.fold, world.ancestry, bridges=())
        assert result.accepted.history_complete is False
        assert store.history_complete(su.state_id) is False

        assert _offer(store, offered[0]) is Admission.ACCEPTED
        assert store.history_complete(su.state_id) is False  # one edge still open
        assert _offer(store, offered[1]) is Admission.ACCEPTED
        assert store.history_complete(su.state_id) is True


def test_locator_lookup_uses_the_edge_index():
    """Structural evidence, not timing: the planner searches the
    ``(child, parent)`` index rather than scanning the table."""
    world, store, (s0, sa, sb, su) = _loaded_union_store()
    with store:
        plan = " ".join(
            str(row)
            for row in store.db.execute(
                "EXPLAIN QUERY PLAN SELECT bridge_id FROM keycontrol_bridge"
                " WHERE child_state_id = ? AND parent_state_id = ?",
                (su.state_id, sa.state_id),
            )
        )
        assert "SCAN" not in plan
        assert "keycontrol_bridge_edge" in plan


def test_pruned_body_is_never_hydrated_as_a_bridge():
    """A NULL body is LOCALLY UNAVAILABLE everywhere: not in
    ``accepted_bridges``, not complete, and never followed by recovery."""
    world, store, (s0, sa, sb, su) = _loaded_union_store()
    with store:
        (su_sa,) = [
            b for b in _bridges_for(world, su) if b.parent_state_id == sa.state_id
        ]
        store.prune_bridge_body(su_sa.bridge_id)

        assert store.get_bridge(su_sa.bridge_id) is None
        assert su_sa.bridge_id not in {b.bridge_id for b in store.accepted_bridges()}
        assert store.history_complete(su.state_id) is False
        recovered = store.recover_ancestors(
            su.state_id, world.held(world.member(0))[su.state_id]
        )
        assert sa.state_id not in recovered  # the pruned edge is not followed
        assert sb.state_id in recovered  # the other one still is

        # The row itself survives as the audit anchor.
        assert _bridge_row_count(store) == 4


# -- store-backed recovery ---------------------------------------------------------------


def test_holder_of_a_child_secret_recovers_every_ancestor():
    world, store, (s0, sa, sb, su) = _loaded_union_store()
    with store:
        m0 = world.member(0)
        recovered = store.recover_ancestors(su.state_id, world.held(m0)[su.state_id])
        assert set(recovered) == {
            su.state_id, sa.state_id, sb.state_id, s0.state_id
        }
        # Every recovered secret matched its parent descriptor's commitment
        # inside ``recover_ancestors``; re-derive to say so explicitly.
        for state_id, secret in recovered.items():
            descriptor = store.get(state_id)
            assert state_mod.compute_secret_commitment(
                descriptor.genesis_id, descriptor.domain_id,
                descriptor.state_nonce, secret,
            ) == descriptor.secret_commitment
        assert recovered[s0.state_id] == world.held(m0)[s0.state_id]


def test_holder_of_a_parent_secret_recovers_nothing_newer():
    """The property the whole construction exists for: child reaches parent,
    parent never reaches child."""
    world, store, (s0, sa, sb, su) = _loaded_union_store()
    with store:
        m0 = world.member(0)
        recovered = store.recover_ancestors(s0.state_id, world.held(m0)[s0.state_id])
        assert set(recovered) == {s0.state_id}
        for newer in (sa, sb, su):
            assert newer.state_id not in recovered


def test_poisoned_ciphertext_raises_at_recovery_not_at_admission():
    """The store holds ``secret_commitment`` and never state secrets, so it
    cannot derive ``edge_key`` and cannot authenticate a body at admission."""
    world, s0, sa, sb, su = _union_world()
    honest = {b.parent_state_id: b for b in _bridges_for(world, su)}
    poisoned = dataclasses.replace(
        honest[sa.state_id],
        encrypted_parent_secret=honest[sb.state_id].encrypted_parent_secret,
    )
    poisoned = dataclasses.replace(
        poisoned, signature=world.member(0).sign_hex(poisoned.signing_input())
    )
    with KeyControlStore(":memory:") as store:
        for descriptor in (s0, sa, sb):
            store.accept_state(
                descriptor, world.fold, world.ancestry,
                bridges=_bridges_for(world, descriptor),
            )
        store.accept_state(su, world.fold, world.ancestry)
        assert _offer(store, poisoned) is Admission.ACCEPTED  # admitted, unread
        with pytest.raises(BridgeError):
            store.recover_ancestors(
                su.state_id, world.held(world.member(0))[su.state_id]
            )


def test_bridge_opening_to_a_wrong_secret_raises_at_recovery():
    """The other poisoning: the body opens, but to a secret that fails its
    parent descriptor's commitment."""
    world, s0, sa, sb, su = _union_world()
    m0 = world.member(0)
    wrong = bridge_mod.create(
        m0,
        genesis_id=su.genesis_id,
        domain_id=su.domain_id,
        child_state_id=su.state_id,
        parent_state_id=sa.state_id,
        child_state_secret=world.held(m0)[su.state_id],
        parent_state_secret=os.urandom(32),  # not sa's secret
        authority_heads=su.authority_heads,
    )
    with KeyControlStore(":memory:") as store:
        for descriptor in (s0, sa, sb):
            store.accept_state(
                descriptor, world.fold, world.ancestry,
                bridges=_bridges_for(world, descriptor),
            )
        store.accept_state(su, world.fold, world.ancestry)
        assert _offer(store, wrong) is Admission.ACCEPTED
        with pytest.raises(CommitmentError):
            store.recover_ancestors(su.state_id, world.held(m0)[su.state_id])


# -- durability ---------------------------------------------------------------------------


def test_store_reopens_with_bridges_and_the_edge_index_intact():
    path = org_ledger_db_path("acme")
    world, store, (s0, sa, sb, su) = _loaded_union_store(path)
    store.close()

    with KeyControlStore(path) as reopened:
        for bridge in world.stores.kc.bridges:
            got = reopened.get_bridge(bridge.bridge_id)
            assert got is not None and got.to_json() == bridge.to_json()
        locator = reopened.bridge_locator(su.state_id, sa.state_id)
        assert locator is not None and len(locator.body_present) == 1
        assert reopened.history_complete(su.state_id) is True
        assert set(
            reopened.recover_ancestors(
                su.state_id, world.held(world.member(0))[su.state_id]
            )
        ) == {su.state_id, sa.state_id, sb.state_id, s0.state_id}


def test_stored_state_wraps_accepted_state(world):
    """``StoredState`` WRAPS ``AcceptedState`` — it does not extend it —
    and direct ``acceptance.accept_state`` callers are unchanged."""
    descriptor = _minted(world)
    with KeyControlStore(":memory:") as store:
        result = store.accept_state(descriptor, world.fold, world.ancestry)
        assert isinstance(result, StoredState)
        assert not isinstance(result, acceptance.AcceptedState)
        assert isinstance(result.accepted, acceptance.AcceptedState)
        assert result.accepted.descriptor == descriptor
        assert result.accepted.creator_member == descriptor.creator_persona
    direct = acceptance.accept_state(descriptor, world.fold, world.ancestry)
    assert isinstance(direct, acceptance.AcceptedState)
    assert direct.creator_member == result.accepted.creator_member


def test_unknown_state_has_no_history_reading():
    with KeyControlStore(":memory:") as store:
        with pytest.raises(UnknownStateError):
            store.history_complete("cd" * 32)


# ==== the pending (dependency-deferral) store ===========================================
#
# The first place a deferred record accumulates durably, so it is observable
# from the moment it exists. COUNTING AND EXPOSING, not judgement: a record
# held pending is either legitimately early or deliberately unresolvable and
# the store cannot tell them apart.


def test_pending_records_are_enumerable_and_countable_by_family_and_dependency():
    awaited = os.urandom(32).hex()
    with KeyControlStore(":memory:") as store:
        waiting = [_stray_bridge(child_id=awaited) for _ in range(2)]
        unrelated = _stray_bridge()
        for bridge in (*waiting, unrelated):
            assert _offer(store, bridge) is Admission.DEFERRED

        assert store.pending_count() == 3
        assert store.pending_count(record_type=RECORD_TYPE_BRIDGE) == 3
        assert store.pending_count(
            dependency_kind="state", dependency_id=awaited
        ) == 2
        assert store.pending_count(
            record_type=RECORD_TYPE_BRIDGE,
            dependency_kind="state",
            dependency_id=awaited,
        ) == 2

        infos = store.pending_records(dependency_kind="state", dependency_id=awaited)
        assert {i.claimed_id for i in infos} == {b.bridge_id for b in waiting}
        assert {i.record_type for i in infos} == {RECORD_TYPE_BRIDGE}
        assert {i.unmet_dependency_id for i in infos} == {awaited}


def test_pending_records_do_not_hydrate_wire_bodies():
    counters = StoreCounters()
    with KeyControlStore(":memory:", counters=counters) as store:
        for _ in range(5):
            _offer(store, _stray_bridge())
        counters.wire_hydrations = 0
        assert len(store.pending_records()) == 5
        assert counters.wire_hydrations == 0


def test_first_held_at_ms_is_immutable_across_retry_and_reopen(monkeypatch):
    """A refreshed timestamp makes age useless exactly when it matters."""
    clock = {"ms": 1_000}
    monkeypatch.setattr(keycontrol, "_now_ms", lambda: clock["ms"])
    path = org_ledger_db_path("acme")
    store = KeyControlStore(path)
    bridge = _stray_bridge()
    assert _offer(store, bridge) is Admission.DEFERRED
    [info] = store.pending_records()
    assert info.first_held_at_ms == 1_000

    clock["ms"] = 9_999
    store.retry_pending("state", bridge.child_state_id)  # a retry pass
    assert _offer(store, bridge) is Admission.DEFERRED  # a redelivery
    assert store.pending_records()[0].first_held_at_ms == 1_000
    store.close()

    with KeyControlStore(path) as reopened:
        assert reopened.pending_records()[0].first_held_at_ms == 1_000


def test_first_delivery_peer_id_is_the_authenticated_peer_or_null():
    with KeyControlStore(":memory:") as store:
        anonymous = _stray_bridge()
        _offer(store, anonymous)
        [info] = store.pending_records(dependency_id=anonymous.child_state_id)
        # NOT the issuer field, NOT an address guess, NOT the distribution key.
        assert info.first_delivery_peer_id is None

        attributed = _stray_bridge()
        _offer(store, attributed, delivery_peer_id="peer:authenticated-7")
        [info] = store.pending_records(dependency_id=attributed.child_state_id)
        assert info.first_delivery_peer_id == "peer:authenticated-7"

        # Redelivery must not overwrite the first provenance.
        _offer(store, attributed, delivery_peer_id="peer:impostor")
        [info] = store.pending_records(dependency_id=attributed.child_state_id)
        assert info.first_delivery_peer_id == "peer:authenticated-7"


def test_a_record_cannot_set_its_own_delivery_provenance():
    """Provenance smuggling: a sender-controlled field never reaches
    ``first_delivery_peer_id`` — canonical parse refuses the wire outright."""
    bridge = _stray_bridge()
    payload = json.loads(bridge.to_json())
    payload["first_delivery_peer_id"] = "peer:i-said-so"
    wire = canonical_json(payload)
    with KeyControlStore(":memory:") as store:
        with pytest.raises(MalformedRecordError):
            store.accept_bridge(record_id(wire), wire)
        assert store.pending_count() == 0


def test_nothing_is_evicted_automatically():
    with KeyControlStore(":memory:") as store:
        bridge = _stray_bridge()
        _offer(store, bridge)
        for _ in range(64):
            store.retry_pending("state", bridge.child_state_id)
        assert store.pending_count() == 1
        assert store.pending_usage().rows == 1
        assert store.pending_records()[0].claimed_id == bridge.bridge_id


def test_at_the_bound_an_unrelated_record_is_refused_non_durably_and_converges():
    """LOSSLESSNESS is what justifies first-arrival behaviour at the resource
    edge: nothing is discarded, the refusal is not durable, and the record
    converges once capacity frees."""
    world, s0, sa, sb, su = _union_world()
    (su_sa,) = [b for b in _bridges_for(world, su) if b.parent_state_id == sa.state_id]
    limits = PendingLimits(max_rows=1, max_bytes=MAX_PENDING_BYTES)
    with KeyControlStore(":memory:", pending_limits=limits) as store:
        for descriptor in (s0, sa, sb):
            store.accept_state(descriptor, world.fold, world.ancestry)
        squatter = _stray_bridge()
        assert _offer(store, squatter) is Admission.DEFERRED

        assert _offer(store, su_sa) is Admission.RETRY_LATER_CAPACITY
        assert store.pending_count() == 1  # evicted nothing
        assert store.pending_records()[0].claimed_id == squatter.bridge_id

        store.drop_pending(RECORD_TYPE_BRIDGE, squatter.bridge_id)  # capacity frees
        assert _offer(store, su_sa) is Admission.DEFERRED  # now durably held
        assert store.pending_count() == 1

        store.accept_state(su, world.fold, world.ancestry)  # its dependency arrives
        assert store.get_bridge(su_sa.bridge_id) is not None
        assert store.pending_count() == 0


def test_the_byte_bound_governs_independently_of_the_row_bound():
    first = _stray_bridge()
    # Room for exactly one wire — the row bound is nowhere near reached.
    limits = PendingLimits(max_rows=1_000, max_bytes=len(first.to_json()))
    with KeyControlStore(":memory:", pending_limits=limits) as store:
        assert _offer(store, first) is Admission.DEFERRED
        usage = store.pending_usage()
        assert usage.bytes == len(first.to_json())
        assert usage.rows == 1 and usage.rows < usage.max_rows
        assert _offer(store, _stray_bridge()) is Admission.RETRY_LATER_CAPACITY


def test_a_duplicate_at_a_full_boundary_stays_deferred_and_consumes_zero():
    """UNIQUENESS IS CHECKED BEFORE CAPACITY."""
    limits = PendingLimits(max_rows=1, max_bytes=MAX_PENDING_BYTES)
    with KeyControlStore(":memory:", pending_limits=limits) as store:
        bridge = _stray_bridge()
        assert _offer(store, bridge) is Admission.DEFERRED
        saturated = store.pending_usage()
        assert _offer(store, bridge) is Admission.DEFERRED  # redelivery, at the bound
        assert store.pending_usage() == saturated
        assert store.pending_count() == 1
        # A genuinely new record at the same boundary is still refused.
        assert _offer(store, _stray_bridge()) is Admission.RETRY_LATER_CAPACITY


def test_a_record_satisfying_a_pending_dependency_is_admitted_at_saturation():
    """The reserved lane: without it, the cap refuses the one thing that
    would drain the queue and the store deadlocks at saturation."""
    world, s0, sa, sb, su = _union_world()
    offered = _bridges_for(world, su)
    limits = PendingLimits(max_rows=len(offered), max_bytes=MAX_PENDING_BYTES)
    with KeyControlStore(":memory:", pending_limits=limits) as store:
        for descriptor in (s0, sa, sb):
            store.accept_state(descriptor, world.fold, world.ancestry)
        for bridge in offered:
            assert _offer(store, bridge) is Admission.DEFERRED
        assert store.pending_usage().rows == store.pending_usage().max_rows

        # su's descriptor SATISFIES the pending dependency and completes
        # normal acceptance: admitted even at the bound.
        store.accept_state(su, world.fold, world.ancestry)
        for bridge in offered:
            assert store.get_bridge(bridge.bridge_id) is not None
        assert store.pending_count() == 0


def test_a_record_whose_own_dependency_is_unmet_gets_no_bypass():
    """Without this qualifier the lane is an unbounded chain: each claimed
    dependency admits the next past the cap forever. NAMING the awaited
    identifier is not SATISFYING it."""
    awaited = os.urandom(32).hex()
    limits = PendingLimits(max_rows=1, max_bytes=MAX_PENDING_BYTES)
    with KeyControlStore(":memory:", pending_limits=limits) as store:
        assert _offer(store, _stray_bridge(child_id=awaited)) is Admission.DEFERRED
        # A second record on the SAME unmet dependency needs its own pending
        # row, so it gets no bypass...
        assert (
            _offer(store, _stray_bridge(child_id=awaited))
            is Admission.RETRY_LATER_CAPACITY
        )
        # ...and neither does an unrelated one.
        assert _offer(store, _stray_bridge()) is Admission.RETRY_LATER_CAPACITY
        assert store.pending_count() == 1


def test_pending_usage_reports_rows_bytes_and_both_maxima_and_survives_reopen():
    path = org_ledger_db_path("acme")
    limits = PendingLimits(max_rows=7, max_bytes=1_000_000)
    store = KeyControlStore(path, pending_limits=limits)
    held = [_stray_bridge() for _ in range(3)]
    for bridge in held:
        _offer(store, bridge)
    expected_bytes = sum(len(b.to_json()) for b in held)
    usage = store.pending_usage()
    assert (usage.rows, usage.bytes) == (3, expected_bytes)
    assert (usage.max_rows, usage.max_bytes) == (7, 1_000_000)
    store.close()

    with KeyControlStore(path, pending_limits=limits) as reopened:
        assert reopened.pending_usage() == usage

    # Removal gives the capacity back, in the same transaction as the delete.
    with KeyControlStore(path, pending_limits=limits) as reopened:
        reopened.drop_pending(RECORD_TYPE_BRIDGE, held[0].bridge_id)
        after = reopened.pending_usage()
        assert after.rows == 2
        assert after.bytes == expected_bytes - len(held[0].to_json())


@pytest.mark.parametrize("backlog", [5, 200])
def test_retry_is_indexed_by_dependency_not_a_full_rescan(backlog):
    """Hold ``k`` rows awaiting one identifier beside a much larger unrelated
    backlog: exactly ``k`` rows are examined as the backlog grows. An INJECTED
    CANDIDATE COUNTER, not wall-clock timing — a timing test passes on a fast
    machine with a quadratic algorithm."""
    counters = StoreCounters()
    awaited = os.urandom(32).hex()
    k = 3
    with KeyControlStore(":memory:", counters=counters) as store:
        for _ in range(k):
            _offer(store, _stray_bridge(child_id=awaited))
        for _ in range(backlog):
            _offer(store, _stray_bridge())
        assert store.pending_count() == k + backlog

        counters.retry_candidates = 0
        counters.wire_hydrations = 0
        store.retry_pending("state", awaited)

        assert counters.retry_candidates == k
        assert counters.wire_hydrations == k  # unrelated wires never hydrated
        assert store.pending_count() == k + backlog  # none of them resolvable yet


def test_concurrent_writers_do_not_both_take_the_last_slot():
    """Capacity check, insert and the counter delta are ONE transaction."""
    path = org_ledger_db_path("acme")
    limits = PendingLimits(max_rows=1, max_bytes=MAX_PENDING_BYTES)
    KeyControlStore(path, pending_limits=limits).close()  # create the schema

    wires = [_stray_bridge().to_json() for _ in range(2)]
    barrier = threading.Barrier(len(wires))
    outcomes, failures, lock = [], [], threading.Lock()

    def contend(wire):
        try:
            store = KeyControlStore(path, pending_limits=limits)
            barrier.wait(timeout=10)
            try:
                outcome = store.accept_bridge(record_id(wire), wire)
            finally:
                store.close()
            with lock:
                outcomes.append(outcome)
        except Exception as exc:  # noqa: BLE001 — reported, not swallowed
            with lock:
                failures.append(exc)

    threads = [threading.Thread(target=contend, args=(w,)) for w in wires]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not failures, failures
    assert sorted(o.value for o in outcomes) == ["deferred", "retry_later_capacity"]
    with KeyControlStore(path, pending_limits=limits) as store:
        assert store.pending_count() == 1
        assert store.pending_usage().rows == 1


def test_a_pending_bridge_terminally_refused_on_retry_is_dropped():
    """Deferral postpones the judgement, it does not waive it: a forged edge
    parked pending is refused the moment the descriptor that judges it
    arrives, and does not sit in the queue forever."""
    world, s0, sa, sb, su = _union_world()
    rogue = _resigned_bridge(world, su, s0)  # s0 is not one of su's signed parents
    counters = StoreCounters()
    with KeyControlStore(":memory:", counters=counters) as store:
        assert _offer(store, rogue) is Admission.DEFERRED
        for descriptor in (s0, sa, sb, su):
            store.accept_state(descriptor, world.fold, world.ancestry)
        assert store.pending_count() == 0
        assert store.get_bridge(rogue.bridge_id) is None
        assert counters.retry_terminal_refusals == 1
