"""Membership-proof riders (auto-3bhy3): hello v3, envelope path, freshness.

Drives the registry's tunnel and HTTP gates with committed-membership
riders against a seeded org: admission with a valid proof, every refusal,
the re-prove control op, the grace-window close (CLOSE_MEMBERSHIP_STALE),
the v1/v2 migration exemption, and the envelope path that replaces the old
rung-2 refusal for persona subjects.
"""

from __future__ import annotations

import contextlib
import json

import pytest
from starlette.websockets import WebSocketDisconnect

from tools.network.idkit import KeyPair, Subject, issue_cert
from tools.network.ledger import membership_commitment as mc
from tools.network.relaykit.frames import CTRL_CHANNEL_ID, FRAME_CTRL, encode_frame, decode_frame
from tools.network.relaykit.hello import (
    SERVING_MACHINE_HELLO_DOMAIN,
    build_tunnel_hello_v3,
)
from tools.network.registry.relay import CLOSE_MEMBERSHIP_STALE, CLOSE_UNAUTHENTICATED

from .conftest import DAY, NOW, ORG, register, sign_request
from .test_tunnel_control import _ctrl, _open_tunnel

GENESIS = "aa" * 32
CKPT_PATH = f"/v1/orgs/{ORG}/membership-checkpoints"


def _seed(client, root, member_pubs, checkpointer_pubs=None):
    record = mc.build_root_checkpoint(
        org=ORG, seq=0, genesis_id=GENESIS, ledger_head=GENESIS,
        members_root_hex=mc.compute_root(member_pubs),
        checkpointers_root_hex=mc.compute_root(checkpointer_pubs or member_pubs),
        ts=NOW, root=root)
    assert client.post(CKPT_PATH, json=record).status_code == 201
    return record


def _advance(client, signer, prev, member_pubs, prev_checkpointers):
    record = mc.build_checkpoint(
        org=ORG, seq=prev["seq"] + 1, prev=mc.checkpoint_hash(prev),
        ledger_head="bb" * 32,
        members_root_hex=mc.compute_root(member_pubs),
        checkpointers_root_hex=mc.compute_root(member_pubs),
        ts=NOW, signer=signer, prev_checkpointer_pubs=prev_checkpointers)
    assert client.post(CKPT_PATH, json=record).status_code == 201
    return record


def _rider(member_pubs, persona_pub, seq):
    index, path = mc.inclusion_proof(member_pubs, persona_pub)
    return {"v": 1, "checkpoint_seq": seq, "index": index, "path": path}


def _persona_serve_cert(root, serve_key, persona_pub):
    return issue_cert(
        root, serve_key.public_hex, scope=("tunnel:serve",), org=ORG,
        subject=Subject("persona", persona_pub),
        not_before=NOW - 100, not_after=NOW + 30 * DAY)


@contextlib.contextmanager
def _open_v3_tunnel(client, clock, root, persona, member_pubs, seq,
                    *, rider=None):
    serve_key, machine_key = KeyPair.generate(), KeyPair.generate()
    cert = _persona_serve_cert(root, serve_key, persona.public_hex)
    hello = build_tunnel_hello_v3(
        serve_key, cert, machine_key=machine_key, org=ORG, ts=clock.now,
        membership_proof=rider or _rider(member_pubs, persona.public_hex, seq),
        machine_hello_domain=SERVING_MACHINE_HELLO_DOMAIN)
    with client.websocket_connect(f"/t/{ORG}") as ws:
        ws.send_text(hello)
        yield ws


@pytest.fixture
def founder():
    return KeyPair.generate()


class TestHelloV3:
    def test_valid_rider_admits(self, client, clock, root, founder):
        register(client, clock, root)
        _seed(client, root, [founder.public_hex])
        with _open_v3_tunnel(client, clock, root, founder,
                             [founder.public_hex], 0) as ws:
            ack = ws.receive_json()
            assert ack["ok"] is True and ack["v"] == 3
            reply = _ctrl(ws, "1" * 32, "create-link",
                          {"target_uuid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                           "target_type": "present"})
            assert reply["ok"] is True

    def test_stale_seq_refused(self, client, clock, root, founder):
        register(client, clock, root)
        seed = _seed(client, root, [founder.public_hex])
        _advance(client, founder, seed, [founder.public_hex],
                 [founder.public_hex])
        stale = _rider([founder.public_hex], founder.public_hex, 0)
        with _open_v3_tunnel(client, clock, root, founder,
                             [founder.public_hex], 0, rider=stale) as ws:
            ack = ws.receive_json()
            assert ack["ok"] is False and "stale" in ack["error"]

    def test_wrong_persona_refused(self, client, clock, root, founder):
        register(client, clock, root)
        _seed(client, root, [founder.public_hex])
        outsider = KeyPair.generate()
        # A proof for the outsider under a set it fabricated: the cert names
        # the outsider, the registry's adopted root does not contain it.
        forged = _rider([outsider.public_hex], outsider.public_hex, 0)
        with _open_v3_tunnel(client, clock, root, outsider,
                             [outsider.public_hex], 0, rider=forged) as ws:
            ack = ws.receive_json()
            assert ack["ok"] is False and "does not verify" in ack["error"]

    def test_no_membership_state_refused(self, client, clock, root, founder):
        register(client, clock, root)
        with _open_v3_tunnel(client, clock, root, founder,
                             [founder.public_hex], 0) as ws:
            ack = ws.receive_json()
            assert ack["ok"] is False and "seed" in ack["error"]


class TestFreshness:
    def test_reprove_restamps_and_ops_continue(self, client, clock, root, founder):
        register(client, clock, root)
        seed = _seed(client, root, [founder.public_hex])
        with _open_v3_tunnel(client, clock, root, founder,
                             [founder.public_hex], 0) as ws:
            assert ws.receive_json()["ok"] is True
            joiner = KeyPair.generate()
            members = sorted([founder.public_hex, joiner.public_hex])
            _advance(client, founder, seed, members, [founder.public_hex])
            reply = _ctrl(ws, "2" * 32, "re-prove-membership",
                          _rider(members, founder.public_hex, 1))
            assert reply["ok"] is True and reply["seq"] == 1
            ok = _ctrl(ws, "3" * 32, "create-link",
                       {"target_uuid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                        "target_type": "present"})
            assert ok["ok"] is True

    def test_stale_past_grace_closes_typed(self, client, clock, root, founder):
        register(client, clock, root)
        seed = _seed(client, root, [founder.public_hex])
        with _open_v3_tunnel(client, clock, root, founder,
                             [founder.public_hex], 0) as ws:
            assert ws.receive_json()["ok"] is True
            _advance(client, founder, seed, [founder.public_hex],
                     [founder.public_hex])
            # First op after the advance: grace begins, the op still runs.
            grace = _ctrl(ws, "4" * 32, "create-link",
                          {"target_uuid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                           "target_type": "present"})
            assert grace["ok"] is True
            clock.advance(120)  # past MEMBERSHIP_REPROVE_GRACE_S
            request = {"id": "5" * 32, "op": "create-link", "args": {
                "target_uuid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "target_type": "present"}}
            ws.send_bytes(encode_frame(
                FRAME_CTRL, CTRL_CHANNEL_ID,
                json.dumps(request).encode("utf-8")))
            with pytest.raises(WebSocketDisconnect) as excinfo:
                ws.receive_bytes()
            assert excinfo.value.code == CLOSE_MEMBERSHIP_STALE

    def test_reprove_allowed_even_when_expired(self, client, clock, root, founder):
        register(client, clock, root)
        seed = _seed(client, root, [founder.public_hex])
        with _open_v3_tunnel(client, clock, root, founder,
                             [founder.public_hex], 0) as ws:
            assert ws.receive_json()["ok"] is True
            _advance(client, founder, seed, [founder.public_hex],
                     [founder.public_hex])
            grace = _ctrl(ws, "6" * 32, "create-link",
                          {"target_uuid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                           "target_type": "present"})
            assert grace["ok"] is True
            clock.advance(120)
            reply = _ctrl(ws, "7" * 32, "re-prove-membership",
                          _rider([founder.public_hex], founder.public_hex, 1))
            assert reply["ok"] is True and reply["seq"] == 1

    def test_v1_tunnel_exempt_during_migration(self, client, clock, root, founder):
        # A v1 hello (no rider) keeps working even as checkpoints advance —
        # the migration window; auto-tmers owns the cutoff.
        with _open_tunnel(client, clock, root) as ws:
            seed = _seed(client, root, [founder.public_hex])
            _advance(client, founder, seed, [founder.public_hex],
                     [founder.public_hex])
            clock.advance(120)
            reply = _ctrl(ws, "8" * 32, "create-link",
                          {"target_uuid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                           "target_type": "present"})
            assert reply["ok"] is True


class TestEnvelopeRider:
    def _persona_session(self, persona, session_key):
        """A persona-signed session cert: persona → session key, the
        post-2026-08-12 shape the root-anchored gate used to refuse."""
        return issue_cert(
            persona, session_key.public_hex, scope=("link:publish",),
            org=ORG, subject=Subject("operator", persona.public_hex),
            not_before=NOW - 100, not_after=NOW + DAY)

    def _renew(self, client, clock, session_key, cert, rider=None):
        envelope = sign_request(
            session_key, "POST", f"/v1/orgs/{ORG}/renew", {},
            ts=clock.now, cert=cert)
        if rider is not None:
            envelope["membership_proof"] = rider
        return client.post(f"/v1/orgs/{ORG}/renew", json=envelope)

    def test_persona_cert_with_rider_authorizes(self, client, clock, root):
        persona, session_key = KeyPair.generate(), KeyPair.generate()
        register(client, clock, root)
        _seed(client, root, [persona.public_hex])
        cert = self._persona_session(persona, session_key)
        rider = _rider([persona.public_hex], persona.public_hex, 0)
        response = self._renew(client, clock, session_key, cert, rider)
        assert response.status_code == 200, response.text

    def test_persona_cert_without_rider_still_refused(self, client, clock, root):
        persona, session_key = KeyPair.generate(), KeyPair.generate()
        register(client, clock, root)
        _seed(client, root, [persona.public_hex])
        cert = self._persona_session(persona, session_key)
        response = self._renew(client, clock, session_key, cert)
        # No rider → the chain is anchored at the bound root and fails hop 1.
        assert response.status_code == 403

    def test_stale_rider_refused(self, client, clock, root):
        persona, session_key = KeyPair.generate(), KeyPair.generate()
        register(client, clock, root)
        seed = _seed(client, root, [persona.public_hex])
        # Advance via a persona checkpoint so seq 0 riders go stale.
        record = mc.build_checkpoint(
            org=ORG, seq=1, prev=mc.checkpoint_hash(seed),
            ledger_head="bb" * 32,
            members_root_hex=seed["members_root"],
            checkpointers_root_hex=seed["checkpointers_root"],
            ts=NOW, signer=persona,
            prev_checkpointer_pubs=[persona.public_hex])
        assert client.post(CKPT_PATH, json=record).status_code == 201
        cert = self._persona_session(persona, session_key)
        rider = _rider([persona.public_hex], persona.public_hex, 0)
        response = self._renew(client, clock, session_key, cert, rider)
        assert response.status_code == 403
        assert "stale" in response.json()["detail"]

    def test_non_member_rider_refused(self, client, clock, root, founder):
        outsider, session_key = KeyPair.generate(), KeyPair.generate()
        register(client, clock, root)
        _seed(client, root, [founder.public_hex])
        cert = self._persona_session(outsider, session_key)
        rider = _rider([outsider.public_hex], outsider.public_hex, 0)
        response = self._renew(client, clock, session_key, cert, rider)
        assert response.status_code == 403
        assert "does not verify" in response.json()["detail"]


class TestFreshnessStateMachine:
    """_membership_freshness unit coverage — the same function gates the
    control dispatch and viewer admission."""

    def _tunnel(self, proven_seq):
        class T:
            org = ORG
        t = T()
        t.proven_seq = proven_seq
        t.membership_stale_since = None
        return t

    def test_transitions(self, client, clock, root, founder, app):
        from tools.network.registry.relay import (
            MEMBERSHIP_REPROVE_GRACE_S,
            _membership_freshness,
        )
        register(client, clock, root)
        store = app.state.store
        exempt = self._tunnel(None)
        assert _membership_freshness(exempt, store, clock.now) == "fresh"
        seed = _seed(client, root, [founder.public_hex])
        proven = self._tunnel(0)
        assert _membership_freshness(proven, store, clock.now) == "fresh"
        _advance(client, founder, seed, [founder.public_hex],
                 [founder.public_hex])
        assert _membership_freshness(proven, store, clock.now) == "grace"
        assert _membership_freshness(
            proven, store, clock.now + MEMBERSHIP_REPROVE_GRACE_S) == "grace"
        assert _membership_freshness(
            proven, store, clock.now + MEMBERSHIP_REPROVE_GRACE_S + 1) == "expired"
        # Re-stamp clears staleness.
        proven.proven_seq = 1
        assert _membership_freshness(proven, store, clock.now) == "fresh"
        assert proven.membership_stale_since is None
