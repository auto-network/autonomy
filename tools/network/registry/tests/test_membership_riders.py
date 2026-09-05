"""Membership-proof riders (auto-3bhy3): hello v3, envelope path, freshness.

Drives the registry's tunnel and HTTP gates with committed-membership
riders against a seeded org: admission with a valid proof, every refusal,
the re-prove control op, the push-then-deadline close (CLOSE_MEMBERSHIP_STALE),
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
import asyncio as _asyncio
from tools.network.registry.relay import (
    CLOSE_MEMBERSHIP_STALE,
    push_reprove_and_enforce,
    _tunnel_behind,
)

from .conftest import DAY, NOW, ORG, register, sign_request
from .test_tunnel_control import _ctrl, _open_tunnel
from tools.network.relaykit.frames import decode_frame


def _ctrl_await(ws, correlation, op, args):
    """Send a control op and return ITS reply, skipping any server-pushed
    frames (e.g. reprove-required) that arrive first — those carry a
    different id and no reply correlation."""
    import json as _json
    from tools.network.relaykit.frames import CTRL_CHANNEL_ID, FRAME_CTRL, encode_frame
    ws.send_bytes(encode_frame(
        FRAME_CTRL, CTRL_CHANNEL_ID,
        _json.dumps({"id": correlation, "op": op, "args": args}).encode("utf-8")))
    for _ in range(8):
        frame = decode_frame(ws.receive_bytes())
        msg = _json.loads(frame.payload.decode("utf-8"))
        if msg.get("id") == correlation:
            return msg
    raise AssertionError("no reply for correlation id")


def _drain_push(ws):
    """Read one server-pushed reprove-required frame; return its seq."""
    import json as _json
    frame = decode_frame(ws.receive_bytes())
    msg = _json.loads(frame.payload.decode("utf-8"))
    assert msg.get("op") == "reprove-required"
    return msg["args"]["seq"]

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


class _FakeTunnel:
    def __init__(self, org, proven_seq):
        self.org = org
        self.proven_seq = proven_seq
        self.persona_pub = "p"
        self.sent = []
        self.closed = None
        self.viewers_closed = None
        self.ws = self

    async def send_frame(self, ftype, chan, payload):
        self.sent.append(payload)

    async def close_all_viewers(self, code):
        self.viewers_closed = code

    async def close(self, code=1000):
        self.closed = code


class _FakeHub:
    def __init__(self, tunnels):
        self._t = tunnels
        self.unregistered = []

    def tunnels_for(self, org):
        return list(self._t)

    def unregister(self, tunnel):
        self.unregistered.append(tunnel)


class _FakeState:
    def __init__(self, seq):
        self.seq = seq


class _FakeStore:
    def __init__(self, seq):
        self._seq = seq

    def get_membership_state(self, org):
        return _FakeState(self._seq)

    def set_seq(self, seq):
        self._seq = seq


class TestReproveOp:
    """The re-prove control op re-stamps a live tunnel and its ops continue —
    a checkpoint advance never blocks the tunnel."""

    def test_reprove_restamps_and_ops_continue(self, client, clock, root, founder):
        register(client, clock, root)
        seed = _seed(client, root, [founder.public_hex])
        with _open_v3_tunnel(client, clock, root, founder,
                             [founder.public_hex], 0) as ws:
            assert ws.receive_json()["ok"] is True
            joiner = KeyPair.generate()
            members = sorted([founder.public_hex, joiner.public_hex])
            _advance(client, founder, seed, members, [founder.public_hex])
            # The registry pushes reprove-required down the live tunnel.
            assert _drain_push(ws) == 1
            # The tunnel keeps serving even while behind (no op-blocking gate).
            still = _ctrl_await(ws, "1" * 32, "create-link",
                                {"target_uuid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                                 "target_type": "present"})
            assert still["ok"] is True
            reply = _ctrl_await(ws, "2" * 32, "re-prove-membership",
                                _rider(members, founder.public_hex, 1))
            assert reply["ok"] is True and reply["seq"] == 1

    def test_reprove_stale_rider_soft_refused(self, client, clock, root, founder):
        register(client, clock, root)
        seed = _seed(client, root, [founder.public_hex])
        with _open_v3_tunnel(client, clock, root, founder,
                             [founder.public_hex], 0) as ws:
            assert ws.receive_json()["ok"] is True
            _advance(client, founder, seed, [founder.public_hex],
                     [founder.public_hex])
            assert _drain_push(ws) == 1
            reply = _ctrl_await(ws, "3" * 32, "re-prove-membership",
                                _rider([founder.public_hex], founder.public_hex, 0))
            # A stale re-prove is a soft {ok:false}, not a close — the connector
            # retries with a fresh proof.
            assert reply["ok"] is False and "stale" in reply["error"]

    def test_v1_tunnel_exempt_during_migration(self, client, clock, root, founder):
        with _open_tunnel(client, clock, root) as ws:
            seed = _seed(client, root, [founder.public_hex])
            _advance(client, founder, seed, [founder.public_hex],
                     [founder.public_hex])
            reply = _ctrl(ws, "8" * 32, "create-link",
                          {"target_uuid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                           "target_type": "present"})
            assert reply["ok"] is True


class TestPushAndDeadline:
    """push_reprove_and_enforce: push to behind tunnels, close only those
    still behind at the deadline; never touch fresh or exempt tunnels."""

    def _run(self, hub, store, org, seq, deadline=0):
        import tools.network.registry.relay as relay
        orig = relay.MEMBERSHIP_REPROVE_DEADLINE_S
        relay.MEMBERSHIP_REPROVE_DEADLINE_S = deadline
        try:
            _asyncio.run(push_reprove_and_enforce(hub, store, org, seq))
        finally:
            relay.MEMBERSHIP_REPROVE_DEADLINE_S = orig

    def test_behind_tunnel_pushed_then_closed(self):
        behind = _FakeTunnel("o", proven_seq=0)
        hub, store = _FakeHub([behind]), _FakeStore(1)
        self._run(hub, store, "o", 1)
        assert behind.sent, "a reprove-required frame was pushed"
        assert behind.closed == CLOSE_MEMBERSHIP_STALE or behind in hub.unregistered
        assert behind.viewers_closed == CLOSE_MEMBERSHIP_STALE

    def test_tunnel_that_reproves_before_deadline_survives(self):
        # An honest member answers the push immediately: model that by having
        # send_frame catch proven_seq up to the pushed set. At the deadline the
        # tunnel is no longer behind, so it is not closed.
        member = _FakeTunnel("o", proven_seq=0)

        async def respond(ftype, chan, payload):
            member.sent.append(payload)
            member.proven_seq = 1

        member.send_frame = respond
        hub, store = _FakeHub([member]), _FakeStore(1)
        self._run(hub, store, "o", 1)
        assert member.sent  # pushed
        assert member not in hub.unregistered  # NOT closed
        assert member.viewers_closed is None

    def test_fresh_and_exempt_tunnels_untouched(self):
        fresh = _FakeTunnel("o", proven_seq=1)
        exempt = _FakeTunnel("o", proven_seq=None)
        hub, store = _FakeHub([fresh, exempt]), _FakeStore(1)
        self._run(hub, store, "o", 1)
        assert not fresh.sent and not exempt.sent
        assert hub.unregistered == []


class TestBehindPredicate:
    def test_predicate(self):
        store = _FakeStore(2)
        assert _tunnel_behind(_FakeTunnel("o", 1), store) is True
        assert _tunnel_behind(_FakeTunnel("o", 2), store) is False
        assert _tunnel_behind(_FakeTunnel("o", None), store) is False


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
