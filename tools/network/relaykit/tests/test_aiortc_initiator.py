from __future__ import annotations

import asyncio

import pytest

from tools.network.idkit import canonical_json
from tools.network.relaykit.aiortc_initiator import (
    NativeIceError,
    _DataChannelTransport,
    _answer,
    _configuration,
    _wait_open,
    upgrade_via_ice,
)
from tools.network.relaykit.ice_signaling import (
    STUN_URL,
    TURN_URLS,
    IceSignalingError,
)


ATTEMPT = "a" * 32
NOW = 4_000_000_000


def config_wire(**changes) -> bytes:
    value = {
        "v": 1,
        "op": "ice.config",
        "attempt_id": ATTEMPT,
        "policy": "relay_only",
        "ice_servers": [
            {"urls": [STUN_URL]},
            {
                "urls": list(TURN_URLS),
                "username": "4000000900:test",
                "credential": "test-password",
                "credentialType": "password",
            },
        ],
        "expires_at": NOW + 900,
    }
    value.update(changes)
    return canonical_json(value)


def answer_wire(**changes) -> bytes:
    value = {
        "v": 1,
        "op": "ice.answer",
        "attempt_id": ATTEMPT,
        "sdp": "v=0\r\nc=IN IP4 0.0.0.0\r\n",
        "candidates": [],
    }
    value.update(changes)
    return canonical_json(value)


def test_configuration_accepts_only_the_frozen_issuer_shape():
    policy, configuration, size = _configuration(
        config_wire(), attempt_id=ATTEMPT, now=NOW
    )
    assert policy == "relay_only"
    assert configuration.expires_at == NOW + 900
    assert size == len(config_wire())

    with pytest.raises(IceSignalingError, match="match this attempt"):
        _configuration(
            config_wire(policy=["relay_only"]),
            attempt_id=ATTEMPT,
            now=NOW,
        )


def test_answer_rejects_candidate_smuggling_in_sdp():
    answer, size = _answer(
        answer_wire(), attempt_id=ATTEMPT, policy="relay_only"
    )
    assert answer.candidates == ()
    assert size == len(answer_wire())

    with pytest.raises(IceSignalingError, match="smuggled ICE candidate"):
        _answer(
            answer_wire(sdp="v=0\r\na=end-of-candidates\r\n"),
            attempt_id=ATTEMPT,
            policy="relay_only",
        )


class FakeChannel:
    def __init__(self, state="connecting"):
        self.readyState = state
        self.bufferedAmount = 0
        self.bufferedAmountLowThreshold = 0
        self.handlers = {}
        self.sent = []
        self.closed = False

    def on(self, name):
        def register(handler):
            self.handlers[name] = handler
            return handler

        return register

    def send(self, payload):
        self.sent.append(payload)

    def close(self):
        self.closed = True
        self.readyState = "closed"


class FakePeer:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_wait_open_catches_transition_after_listener_registration():
    channel = FakeChannel()
    task = asyncio.create_task(_wait_open(channel, timeout=0.5))
    await asyncio.sleep(0)
    channel.readyState = "open"
    channel.handlers["open"]()
    await task


@pytest.mark.asyncio
async def test_datachannel_transport_poison_is_synchronous():
    channel = FakeChannel("open")
    peer = FakePeer()
    transport = _DataChannelTransport(channel, peer)

    await transport.close()
    channel.handlers["message"](b"post-close")
    with pytest.raises(ConnectionError, match="closed"):
        await transport.recv()
    assert channel.closed is True
    assert peer.closed is True


@pytest.mark.asyncio
async def test_remote_datachannel_close_releases_the_peer():
    channel = FakeChannel("open")
    peer = FakePeer()
    transport = _DataChannelTransport(channel, peer)

    channel.handlers["close"]()
    with pytest.raises(ConnectionError, match="closed"):
        await transport.recv()
    await transport.close()
    assert peer.closed is True


@pytest.mark.asyncio
async def test_missing_native_dependency_closes_dedicated_signaling(monkeypatch):
    class Signaling:
        closed = False

        async def close(self):
            self.closed = True

    signaling = Signaling()

    def unavailable():
        raise NativeIceError("native dependency unavailable")

    monkeypatch.setattr(
        "tools.network.relaykit.aiortc_initiator.load_aiortc_modules",
        unavailable,
    )
    with pytest.raises(NativeIceError, match="unavailable"):
        await upgrade_via_ice(
            signaling,
            token="a" * 32,
            root_pub="b" * 64,
            org="c" * 36,
        )
    assert signaling.closed is True
