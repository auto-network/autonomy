"""LedgerStore (F2): round-trip equivalence, rebuild-from-zero, two-layer
L8, checkpoint cold-join, content-address tamper detection, persistence."""

from __future__ import annotations

import random
import sqlite3

import pytest

from tools.network.idkit import KeyPair
from tools.network.ledger import (
    HLC,
    Event,
    Ledger,
    LedgerError,
    LedgerStore,
    SchemaError,
    StoreError,
    TamperError,
    UnknownParentError,
    build_projections,
    fold,
    ledger_state_payload,
    make_event,
    org_ledger_db_path,
    projection_bytes,
)

from .conftest import Sim, random_events


def populated_sim():
    """An org with members, delegations, a revocation, and role structure."""
    sim = Sim()
    sim.role_define(sim.root, "member", ["link:publish"])
    sponsor = KeyPair.generate()
    sim.delegate(sim.root, sponsor, ["invite:member", "link:publish"], redelegate=True)
    ik, persona = KeyPair.generate(), KeyPair.generate()
    invite = sim.invite(sponsor, "member", invite_key=ik)
    sim.claim(invite, ik, persona)
    other = KeyPair.generate()
    g = sim.delegate(sponsor, other, ["link:publish"])
    sim.revoke_event(sponsor, g)
    return sim


def store_from(sim, path=":memory:") -> LedgerStore:
    store = LedgerStore(path)
    store.append_bundle(sim.ledger.events())
    return store


class TestRoundTripEquivalence:
    def test_store_fold_matches_direct_fold(self):
        sim = populated_sim()
        store = store_from(sim)
        direct = fold(sim.ledger)
        via_store = store.fold()
        assert via_store.fingerprint() == direct.fingerprint()
        assert via_store.valid == direct.valid
        assert store.heads() == sim.ledger.heads()

    def test_projections_match_direct_build(self):
        sim = populated_sim()
        store = store_from(sim)
        direct = {
            name: projection_bytes(p)
            for name, p in build_projections(fold(sim.ledger)).items()
        }
        assert store.refresh_projections() == direct
        assert store.load_projections() == direct

    @pytest.mark.parametrize("seed", range(5))
    def test_random_dag_round_trip_any_order(self, seed):
        events = random_events(seed, n=30)
        reference = Ledger()
        reference.ingest(list(events))
        shuffled = list(events)
        random.Random(seed).shuffle(shuffled)
        store = LedgerStore(":memory:")
        store.append_bundle(shuffled)
        assert store.fold().fingerprint() == fold(reference).fingerprint()

    def test_append_wire_round_trip(self):
        sim = populated_sim()
        store = LedgerStore(":memory:")
        # Sim's HLC is strictly causal, so HLC order is a valid topo order.
        for event in sorted(sim.ledger.events(), key=lambda e: e.hlc):
            store.append_wire(event.to_json())
        assert store.fold().fingerprint() == fold(sim.ledger).fingerprint()

    def test_ledger_state_payload_shape(self):
        sim = populated_sim()
        store = store_from(sim)
        state = store.fold()
        doc = ledger_state_payload(
            state, org_uuid=state.org, event_count=len(store)
        )
        assert doc["heads"] == list(store.heads())
        assert doc["fingerprint"] == state.fingerprint()
        assert doc["genesis_id"] == sim.genesis_id
        assert doc["event_count"] == len(sim.ledger)
        assert doc["root_pub"] == sim.root.public_hex


class TestPersistence:
    def test_reopen_from_disk_is_equivalent(self, tmp_path):
        sim = populated_sim()
        db = tmp_path / "org.ledger.db"
        store = store_from(sim, db)
        fp = store.fold().fingerprint()
        projections = store.refresh_projections()
        store.close()

        reopened = LedgerStore(db)
        assert len(reopened) == len(sim.ledger)
        assert reopened.heads() == sim.ledger.heads()
        assert reopened.fold().fingerprint() == fp
        assert reopened.load_projections() == projections

    def test_append_is_idempotent_and_incremental(self, tmp_path):
        sim = populated_sim()
        db = tmp_path / "org.ledger.db"
        store = store_from(sim, db)
        n = len(store)
        store.append(sim.ledger.events()[-1])  # duplicate
        assert len(store) == n
        extra = sim.checkpoint(sim.root)
        store.append(sim.ledger.get(extra))
        store.close()
        reopened = LedgerStore(db)
        assert extra in reopened
        assert reopened.heads() == (extra,)

    def test_append_unknown_parent_rejected_and_not_persisted(self, tmp_path):
        sim = Sim()
        db = tmp_path / "org.ledger.db"
        store = store_from(sim, db)
        orphan = make_event(
            sim.root,
            {"type": "checkpoint", "state_hash": "ab" * 32, "signers": [sim.root.public_hex]},
            ["99" * 32],
            HLC(sim.next_ts()),
        )
        with pytest.raises(UnknownParentError):
            store.append(orphan)
        store.close()
        assert orphan.event_id not in LedgerStore(db)

    def test_org_ledger_db_path(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path))
        assert org_ledger_db_path("autonomy") == tmp_path / "autonomy.db"
        monkeypatch.delenv("AUTONOMY_ORGS_DIR")
        assert org_ledger_db_path("autonomy").name == "autonomy.db"
        assert org_ledger_db_path("autonomy", root=tmp_path).parent == tmp_path


class TestRebuildFromZero:
    def test_rebuilt_projections_are_byte_identical(self):
        sim = populated_sim()
        store = store_from(sim)
        first = store.refresh_projections()
        # wipe the derived views entirely, then refold from the event store
        with store.db:
            store.db.execute("DELETE FROM ledger_projections")
        assert store.load_projections() == {}
        rebuilt = store.rebuild_projections()
        assert rebuilt == first
        assert store.load_projections() == first

    def test_rebuild_reflects_new_events(self):
        sim = populated_sim()
        store = store_from(sim)
        before = store.refresh_projections()
        persona2, ik2 = KeyPair.generate(), KeyPair.generate()
        sponsor = KeyPair.generate()
        sim.delegate(sim.root, sponsor, ["invite:member"])
        invite = sim.invite(sponsor, "member", invite_key=ik2)
        sim.claim(invite, ik2, persona2)
        store.append_bundle(sim.ledger.events())  # idempotent for known ids
        after = store.rebuild_projections()
        assert after != before
        assert persona2.public_hex.encode() in after["roster"]


class TestL8TwoLayer:
    """The same forbidden event must die at BOTH layers independently."""

    def test_layer_one_schema_rejects_at_mint_and_parse(self, sim):
        with pytest.raises(SchemaError):
            make_event(
                sim.root,
                {"type": "content.view", "target": "note-1"},
                [sim.genesis_id],
                HLC(sim.next_ts()),
            )
        # A structurally complete envelope around a non-authority payload
        # dies in the parser with the L8 SchemaError specifically.
        rogue_wire = Event(
            author_key=sim.root.public_hex,
            parents=(sim.genesis_id,),
            hlc=HLC(sim.next_ts()),
            payload={"type": "content.view", "target": "note-1"},
            sig="0" * 128,
        ).to_json()
        store = LedgerStore(":memory:")
        with pytest.raises(SchemaError):
            store.append_wire(rogue_wire)

    def test_layer_two_store_rejects_handcrafted_event_object(self, sim):
        """An Event constructed directly (bypassing the parser) still dies
        at the store boundary."""
        store = store_from(sim)
        rogue = Event(
            author_key=sim.root.public_hex,
            parents=(sim.genesis_id,),
            hlc=HLC(sim.next_ts()),
            payload={"type": "content.view", "target": "note-1"},
            sig="0" * 128,
        )
        with pytest.raises(SchemaError):
            store.append(rogue)
        assert rogue.event_id not in store

    def test_layer_two_sql_check_rejects_raw_insert(self, sim):
        """Even raw SQL cannot smuggle a content-access row into the
        replica: the events table CHECK-constrains the type column."""
        store = store_from(sim)
        with pytest.raises(sqlite3.IntegrityError):
            with store.db:
                store.db.execute(
                    "INSERT INTO ledger_events(event_id, event_type, author_key, hlc_ts,"
                    " hlc_count, wire) VALUES (?, 'content.view', ?, 1, 0, X'00')",
                    ("ff" * 32, sim.root.public_hex),
                )


class TestTamperDetection:
    def test_content_address_mismatch_detected_on_open(self, tmp_path):
        sim = populated_sim()
        db = tmp_path / "org.ledger.db"
        store_from(sim, db).close()

        import json as _json

        from tools.network.ledger.settings_bridge import SET_ID

        raw = sqlite3.connect(db)
        rows = raw.execute(
            'SELECT "key", payload FROM settings WHERE set_id=?', (SET_ID,)
        ).fetchall()
        victim, payload = next(
            (k, p) for k, p in rows if b'"delegate"' in _json.loads(p)["wire"].encode()
        )
        wire = _json.loads(payload)["wire"]
        tampered = wire.replace('"can_redelegate":true', '"can_redelegate":false')
        if tampered == wire:
            tampered = wire.replace('"can_redelegate":false', '"can_redelegate":true')
        with raw:
            raw.execute(
                'UPDATE settings SET payload=? WHERE set_id=? AND "key"=?',
                (_json.dumps({"wire": tampered}), SET_ID, victim),
            )
        raw.close()

        with pytest.raises(TamperError):
            LedgerStore(db)

    def test_heads_cannot_be_lied_about_because_they_are_derived(self, tmp_path):
        """Heads used to be a stored table that a tamper check defended.

        They are no longer stored at all: each event carries its parents
        inside its own signed bytes, so the graph -- and therefore its
        heads -- is computed from the events on every open. There is
        nothing to falsify, which is a stronger property than detecting a
        falsification (design graph://53b5bb04-bc0).
        """
        sim = populated_sim()
        db = tmp_path / "org.ledger.db"
        store_from(sim, db).close()
        with LedgerStore(db) as store:
            expected = set(store.ledger.heads())
        raw = sqlite3.connect(db)
        with raw:
            raw.execute("DELETE FROM ledger_heads")
        raw.close()
        with LedgerStore(db) as store:
            assert set(store.ledger.heads()) == expected


class TestCheckpointColdJoin:
    def build_history_with_checkpoint(self):
        sim = populated_sim()
        parents = sim.ledger.heads()
        state_hash = fold(sim.ledger, heads=parents).fingerprint()
        cp = sim.checkpoint(sim.root, state_hash=state_hash, parents=parents)
        # tail after the checkpoint
        late = KeyPair.generate()
        sim.delegate(sim.root, late, ["tunnel:serve"])
        sim.role_define(sim.root, "guest", [], version=1)
        return sim, cp

    def test_cold_join_equals_full_history_fold(self, tmp_path):
        sim, cp = self.build_history_with_checkpoint()
        bundle = sim.ledger.events()
        cold = LedgerStore.cold_join(tmp_path / "cold.ledger.db", bundle, cp)
        assert cold.fold().fingerprint() == fold(sim.ledger).fingerprint()
        assert cold.verify_checkpoint(cp) is True
        assert cold.load_projections() == {
            name: projection_bytes(p)
            for name, p in build_projections(fold(sim.ledger)).items()
        }

    def test_cold_join_rejects_forged_state_hash(self, tmp_path):
        sim = populated_sim()
        forged = sim.checkpoint(sim.root, state_hash="99" * 32)
        with pytest.raises(TamperError):
            LedgerStore.cold_join(tmp_path / "x.ledger.db", sim.ledger.events(), forged)

    def test_cold_join_rejects_truncated_history(self, tmp_path):
        sim, cp = self.build_history_with_checkpoint()
        bundle = sim.ledger.events()
        # drop one non-genesis, non-checkpoint interior event
        victim = next(
            e for e in bundle if e.type == "delegate" and e.event_id != cp
        )
        truncated = [e for e in bundle if e.event_id != victim.event_id]
        with pytest.raises((LedgerError, TamperError)):
            LedgerStore.cold_join(tmp_path / "y.ledger.db", truncated, cp)

    def test_cold_join_requires_checkpoint_in_bundle(self, tmp_path):
        sim, cp = self.build_history_with_checkpoint()
        bundle = [e for e in sim.ledger.events() if e.event_id != cp]
        # removing the checkpoint may orphan its descendants; both failure
        # shapes (unresolvable bundle / missing checkpoint) are acceptable
        with pytest.raises((LedgerError, TamperError, StoreError)):
            LedgerStore.cold_join(tmp_path / "z.ledger.db", bundle, cp)

    def test_checkpoint_state_hash_helper_matches_fold(self):
        sim = populated_sim()
        store = store_from(sim)
        parents = store.heads()
        assert store.checkpoint_state_hash(parents) == fold(
            sim.ledger, heads=parents
        ).fingerprint()

    def test_verify_checkpoint_rejects_non_checkpoint(self):
        sim = populated_sim()
        store = store_from(sim)
        delegate_id = next(e.event_id for e in store.events() if e.type == "delegate")
        with pytest.raises(StoreError):
            store.verify_checkpoint(delegate_id)
