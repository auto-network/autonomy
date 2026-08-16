"""KeyControlStore — StorageStateDescriptor persistence + ancestry traversal.

The production store (``keycontrol.py``) that ``1e005d5c-c11`` §1b names,
exercised through the REAL acceptance layer and the REAL authority fold
via the conftest ``World``. Covers round-trip, content-addressed dedupe,
content-address tamper refusal, acceptance-failure propagation, the
storage-DAG ``ancestry`` closure (including its fail-closed refusal of an
unseen identifier), disk reopen, and co-location beside the ledger.
"""

from __future__ import annotations

import dataclasses
import sqlite3

import pytest

from tools.network.idkit import KeyPair
from tools.network.ledger import LedgerStore, org_ledger_db_path
from tools.network.storagekit import (
    MalformedRecordError,
    RecordSignatureError,
    acceptance,
    select_current_credential,
    state as state_mod,
)
from tools.network.storagekit.acceptance import (
    AuthorityError,
    FrontierRecencyError,
    LossCoverageError,
    ScopeError,
)
from tools.network.storagekit.credentials import build as build_credential
from tools.network.storagekit.keycontrol import (
    KeyControlStore,
    TamperError,
    UnknownStateError,
)

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
