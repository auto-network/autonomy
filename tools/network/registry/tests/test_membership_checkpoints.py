"""Committed-membership checkpoint plane (auto-1wxet).

The registry adopts membership checkpoints by induction from a root-signed
seed and stores one verified tuple per org (graph://da0dd9fb-e75). Covers
the bead's acceptance list: seed adoption, in-order advance, every refusal,
reset supersession, the exclusionary-capture recovery and additive-forgery
self-heal scenarios end to end, and byte-identical replay from the
accepted-checkpoint history.
"""

from __future__ import annotations

import pytest

from tools.network.idkit import KeyPair
from tools.network.ledger import membership_commitment as mc

from .conftest import NOW, ORG, register

GENESIS = "aa" * 32
PATH = f"/v1/orgs/{ORG}/membership-checkpoints"


def seed_record(root, member_pubs, checkpointer_pubs=None, *, seq=0, ts=NOW):
    return mc.build_root_checkpoint(
        org=ORG, seq=seq, genesis_id=GENESIS, ledger_head=GENESIS,
        members_root_hex=mc.compute_root(member_pubs),
        checkpointers_root_hex=mc.compute_root(checkpointer_pubs or member_pubs),
        ts=ts, root=root)


def member_record(signer, prev_record, prev_checkpointer_pubs, member_pubs,
                  checkpointer_pubs=None, *, seq=None, ts=NOW):
    return mc.build_checkpoint(
        org=ORG,
        seq=prev_record["seq"] + 1 if seq is None else seq,
        prev=mc.checkpoint_hash(prev_record),
        ledger_head="bb" * 32,
        members_root_hex=mc.compute_root(member_pubs),
        checkpointers_root_hex=mc.compute_root(checkpointer_pubs or member_pubs),
        ts=ts, signer=signer,
        prev_checkpointer_pubs=prev_checkpointer_pubs)


@pytest.fixture
def founder():
    return KeyPair.generate()


@pytest.fixture
def seeded(client, clock, root, founder):
    """Registered org with an adopted founder-only seed; returns the seed."""
    register(client, clock, root)
    seed = seed_record(root, [founder.public_hex])
    assert client.post(PATH, json=seed).status_code == 201
    return seed


class TestAdoption:
    def test_seed_accepted_and_state_readable(self, client, seeded, founder):
        state = client.get(f"/v1/orgs/{ORG}/membership").json()
        assert state["seq"] == 0
        assert state["members_root"] == mc.compute_root([founder.public_hex])
        assert state["ledger_head"] == GENESIS

    def test_get_before_seed_is_404(self, client, clock, root):
        register(client, clock, root)
        response = client.get(f"/v1/orgs/{ORG}/membership")
        assert response.status_code == 404
        assert "seed" in response.json()["detail"]

    def test_member_chain_advances(self, client, seeded, founder):
        joiner = KeyPair.generate()
        cp1 = member_record(founder, seeded, [founder.public_hex],
                            [founder.public_hex, joiner.public_hex],
                            [founder.public_hex])
        response = client.post(PATH, json=cp1)
        assert response.status_code == 201
        assert client.get(f"/v1/orgs/{ORG}/membership").json()["seq"] == 1


class TestRefusals:
    def test_member_signed_without_seed(self, client, clock, root, founder):
        register(client, clock, root)
        fake_prev = seed_record(root, [founder.public_hex])
        cp = member_record(founder, fake_prev, [founder.public_hex],
                           [founder.public_hex])
        response = client.post(PATH, json=cp)
        assert response.status_code == 403
        assert "seed" in response.json()["detail"]

    def test_seq_skip(self, client, seeded, founder):
        cp = member_record(founder, seeded, [founder.public_hex],
                           [founder.public_hex], seq=3)
        response = client.post(PATH, json=cp)
        assert response.status_code == 403
        assert "exactly one" in response.json()["detail"]

    def test_broken_prev_linkage(self, client, seeded, founder):
        detached = dict(seeded, ledger_head="cc" * 32)  # not the adopted record
        cp = member_record(founder, detached, [founder.public_hex],
                           [founder.public_hex])
        response = client.post(PATH, json=cp)
        assert response.status_code == 403
        assert "hash-link" in response.json()["detail"]

    def test_signer_not_a_checkpointer_under_stored_root(self, client, seeded):
        stranger = KeyPair.generate()
        # The stranger builds a self-consistent proof — under a set the
        # registry never adopted. The stored checkpointers_root wins.
        forged_prev = dict(seeded,
                           checkpointers_root=mc.compute_root([stranger.public_hex]))
        cp = mc.build_checkpoint(
            org=ORG, seq=1, prev=mc.checkpoint_hash(seeded),
            ledger_head="bb" * 32,
            members_root_hex=seeded["members_root"],
            checkpointers_root_hex=seeded["checkpointers_root"],
            ts=NOW, signer=stranger,
            prev_checkpointer_pubs=[stranger.public_hex])
        del forged_prev  # the registry never sees it; the proof must fail
        response = client.post(PATH, json=cp)
        assert response.status_code == 403
        assert "does not verify" in response.json()["detail"]

    def test_tampered_field_fails_signature(self, client, seeded, founder):
        cp = member_record(founder, seeded, [founder.public_hex],
                           [founder.public_hex])
        cp["members_root"] = "0" * 64
        response = client.post(PATH, json=cp)
        assert response.status_code == 403
        assert "signature" in response.json()["detail"]

    def test_root_reset_replay_refused(self, client, seeded, root, founder):
        stale = seed_record(root, [founder.public_hex], seq=0, ts=NOW + 5)
        response = client.post(PATH, json=stale)
        assert response.status_code == 403
        assert "cannot replay" in response.json()["detail"]

    def test_unknown_org_is_404(self, client, root, founder):
        seed = seed_record(root, [founder.public_hex])
        assert client.post(PATH, json=seed).status_code == 404

    def test_expired_binding_is_410(self, client, clock, root, founder):
        register(client, clock, root, ttl=3600)
        clock.advance(7200)
        seed = seed_record(root, [founder.public_hex])
        assert client.post(PATH, json=seed).status_code == 410

    def test_org_path_mismatch_is_400(self, client, seeded, root, founder):
        other = mc.build_root_checkpoint(
            org="22222222-2222-4222-8222-222222222222", seq=1,
            genesis_id=GENESIS, ledger_head=GENESIS,
            members_root_hex=seeded["members_root"],
            checkpointers_root_hex=seeded["checkpointers_root"],
            ts=NOW, root=root)
        response = client.post(PATH, json=other)
        assert response.status_code == 400
        assert "match the path org" in response.json()["detail"]


class TestRecovery:
    def test_reset_supersedes_and_chain_continues_from_it(
            self, client, seeded, root, founder):
        successor = KeyPair.generate()
        reset = seed_record(root, [successor.public_hex], seq=5)
        assert client.post(PATH, json=reset).status_code == 201
        state = client.get(f"/v1/orgs/{ORG}/membership").json()
        assert state["seq"] == 5
        assert state["members_root"] == mc.compute_root([successor.public_hex])
        # The old chain tip is dead: a record chaining to the pre-reset seed
        # no longer hash-links.
        stale = member_record(founder, seeded, [founder.public_hex],
                              [founder.public_hex])
        assert client.post(PATH, json=stale).status_code == 403
        # The chain continues from the reset.
        cp6 = member_record(successor, reset, [successor.public_hex],
                            [successor.public_hex])
        assert client.post(PATH, json=cp6).status_code == 201

    def test_exclusionary_capture_recovered_by_reset(self, client, clock, root):
        rogue, honest = KeyPair.generate(), KeyPair.generate()
        both = sorted([rogue.public_hex, honest.public_hex])
        register(client, clock, root)
        seed = seed_record(root, both)
        assert client.post(PATH, json=seed).status_code == 201
        # The rogue checkpointer excludes the honest one.
        capture = member_record(rogue, seed, both, both, [rogue.public_hex])
        assert client.post(PATH, json=capture).status_code == 201
        # The honest checkpointer can no longer advance the chain.
        healing = member_record(honest, capture, both, both)
        assert client.post(PATH, json=healing).status_code == 403
        # Recovery: ledger removal first (off-registry), then a root-signed
        # reset pinning the post-removal set — which excludes the rogue.
        reset = seed_record(root, [honest.public_hex], seq=2)
        assert client.post(PATH, json=reset).status_code == 201
        # The rogue is out: they cannot advance the reset chain.
        rogue_again = member_record(rogue, reset, [rogue.public_hex],
                                    [rogue.public_hex])
        assert client.post(PATH, json=rogue_again).status_code == 403
        # The honest checkpointer can.
        after = member_record(honest, reset, [honest.public_hex],
                              [honest.public_hex])
        assert client.post(PATH, json=after).status_code == 201

    def test_additive_forgery_healed_by_honest_checkpoint(self, client, clock, root):
        rogue, honest, fake = KeyPair.generate(), KeyPair.generate(), KeyPair.generate()
        both = sorted([rogue.public_hex, honest.public_hex])
        register(client, clock, root)
        seed = seed_record(root, both)
        assert client.post(PATH, json=seed).status_code == 201
        # Rogue smuggles a fake member but leaves the checkpointer set intact.
        forged = member_record(rogue, seed, both,
                               both + [fake.public_hex], both)
        assert client.post(PATH, json=forged).status_code == 201
        # Any honest checkpointer heals with the correct roots — no root, no
        # ceremony, just the next checkpoint.
        healed = member_record(honest, forged, both, both, both)
        assert client.post(PATH, json=healed).status_code == 201
        state = client.get(f"/v1/orgs/{ORG}/membership").json()
        assert state["members_root"] == mc.compute_root(both)


class TestReplay:
    def test_rebuild_reproduces_stored_state(self, app, client, seeded, root, founder):
        cp1 = member_record(founder, seeded, [founder.public_hex],
                            [founder.public_hex])
        assert client.post(PATH, json=cp1).status_code == 201
        reset = seed_record(root, [founder.public_hex], seq=7)
        assert client.post(PATH, json=reset).status_code == 201
        cp8 = member_record(founder, reset, [founder.public_hex],
                            [founder.public_hex])
        assert client.post(PATH, json=cp8).status_code == 201

        store = app.state.store
        rebuilt = store.rebuild_membership_state(ORG, root.public_hex)
        assert rebuilt == store.get_membership_state(ORG).checkpoint
        assert rebuilt["seq"] == 8
