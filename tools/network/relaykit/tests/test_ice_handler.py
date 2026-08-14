from __future__ import annotations

import asyncio
import json

import pytest

from tools.network.relaykit.ice_handler import IceRoutingHandler
from tools.network.relaykit.ice_signaling import (
    IceCapacity,
    IceConfiguration,
    IceSignalingError,
    STUN_URL,
    TURN_URLS,
)


TOKEN = "a" * 32
ATTEMPT = "b" * 32


def configuration():
    return IceConfiguration(
        ice_servers=(
            {"urls": [STUN_URL]},
            {
                "urls": list(TURN_URLS),
                "username": "2000:test",
                "credential": "test-credential",
                "credentialType": "password",
            },
        ),
        expires_at=4_000_000_000,
    )


class UnusedResponder:
    async def answer(self, offer, *, timeout):  # pragma: no cover - this suite stops at config
        raise AssertionError("offer was not expected")


def router(*, policy="direct_allowed", application_handler=None):
    async def default_application(token, raw):
        return b"app:" + raw

    return IceRoutingHandler(
        application_handler or default_application,
        policy_provider=lambda token: policy,
        configuration_provider=lambda token, selected: configuration(),
        responder_factory_provider=lambda token: (
            lambda selected, policy: UnusedResponder()
        ),
        capacity=IceCapacity(4, per_token_limit=2),
    )


@pytest.mark.asyncio
async def test_first_message_selects_application_once_and_removes_ice_deadline():
    channel = router().for_channel(TOKEN)
    assert channel.receive_timeout() == 8.0
    assert await channel(TOKEN, b'{"v":1,"op":"fetch"}') == (
        b'app:{"v":1,"op":"fetch"}'
    )
    assert channel.mode == "application"
    assert channel.receive_timeout() is None
    # An ICE-looking later message stays application data. One connection can
    # never switch capabilities after its first encrypted request.
    later = json.dumps({"v": 1, "op": "ice.begin", "attempt_id": ATTEMPT}).encode()
    assert await channel(TOKEN, later) == b"app:" + later


@pytest.mark.asyncio
async def test_ice_policy_comes_from_provider_not_the_viewer():
    channel = router(policy="relay_only").for_channel(TOKEN)
    begin = json.dumps({"v": 1, "op": "ice.begin", "attempt_id": ATTEMPT}).encode()
    response = json.loads(await channel(TOKEN, begin))
    assert response["op"] == "ice.config"
    assert response["policy"] == "relay_only"
    assert channel.mode == "ice"
    assert channel.on_response_sent() is False
    await channel.aclose()


@pytest.mark.asyncio
async def test_invalid_or_revoked_grant_policy_closes_before_configuration():
    channel = router(policy=None).for_channel(TOKEN)
    begin = json.dumps({"v": 1, "op": "ice.begin", "attempt_id": ATTEMPT}).encode()
    with pytest.raises(IceSignalingError, match="verified grant"):
        await channel(TOKEN, begin)
    await channel.aclose()


@pytest.mark.asyncio
async def test_token_cannot_change_after_the_channel_handshake():
    channel = router().for_channel(TOKEN)
    with pytest.raises(IceSignalingError, match="token changed"):
        await channel("c" * 32, b'{"v":1,"op":"fetch"}')


@pytest.mark.asyncio
async def test_slow_grant_resolution_is_inside_the_ice_start_deadline():
    async def stalled_policy(_token):
        await asyncio.Event().wait()

    handler = IceRoutingHandler(
        lambda token, raw: raw,
        policy_provider=stalled_policy,
        configuration_provider=lambda token, selected: configuration(),
        responder_factory_provider=lambda token: (
            lambda selected, policy: UnusedResponder()
        ),
        capacity=IceCapacity(4, per_token_limit=2),
        first_message_timeout=0.01,
    )
    channel = handler.for_channel(TOKEN)
    begin = json.dumps({"v": 1, "op": "ice.begin", "attempt_id": ATTEMPT}).encode()
    with pytest.raises(IceSignalingError, match="policy resolution timed out"):
        await channel(TOKEN, begin)
    assert handler.capacity.active == 0
    await channel.aclose()
