from __future__ import annotations

import json

import pytest

from tools.dashboard import link_serving
from tools.network.idkit import KeyPair, Subject, issue_cert
from tools.network.relaykit.ice_signaling import (
    IceCapacity,
    IceConfiguration,
    IceSignalingError,
    STUN_URL,
    TURN_URLS,
)
from tools.network.relaykit.connector import Publisher


TOKEN = "a" * 32
ATTEMPT = "b" * 32


def configuration():
    return IceConfiguration(
        ice_servers=(
            {"urls": [STUN_URL]},
            {
                "urls": list(TURN_URLS),
                "username": "4000000000:test",
                "credential": "test-credential",
                "credentialType": "password",
            },
        ),
        expires_at=4_000_000_000,
    )


def viewer_credentials():
    root = KeyPair.generate()
    key = KeyPair.generate()
    cert = issue_cert(
        root,
        key.public_hex,
        scope=("tunnel:serve",),
        org="test-org",
        subject=Subject("operator", key.public_hex),
        not_before=900,
        not_after=4000,
    )
    return key, cert


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("meta", "expected"),
    [({}, "direct_allowed"), ({"ice_policy": "relay_only"}, "relay_only")],
)
async def test_ice_policy_is_derived_only_from_the_verified_local_grant(
    monkeypatch, meta, expected
):
    monkeypatch.setattr(
        link_serving,
        "check_grant",
        lambda token, **kwargs: {"token": token, "meta": meta},
    )
    key, cert = viewer_credentials()
    handler = link_serving.make_ice_grant_handler(
        "test-org",
        configuration_provider=lambda token, policy: configuration(),
        key=key,
        channel_cert=cert,
        peer_runtime=object(),
        signaling_capacity=IceCapacity(4, per_token_limit=2),
        publisher=Publisher(),
        modules=object(),
        now=lambda: 1000,
    )
    channel = handler.for_channel(TOKEN)
    reply = json.loads(await channel(
        TOKEN,
        json.dumps({"v": 1, "op": "ice.begin", "attempt_id": ATTEMPT}).encode(),
    ))
    assert reply["policy"] == expected
    await channel.aclose()


@pytest.mark.asyncio
async def test_revoked_grant_cannot_obtain_ice_configuration(monkeypatch):
    monkeypatch.setattr(link_serving, "check_grant", lambda token, **kwargs: None)
    handler = link_serving.make_ice_grant_handler(
        "test-org",
        configuration_provider=lambda token, policy: configuration(),
        key=object(),
        channel_cert=object(),
        peer_runtime=object(),
        signaling_capacity=IceCapacity(4, per_token_limit=2),
        publisher=Publisher(),
        modules=object(),
        now=lambda: 1000,
    )
    channel = handler.for_channel(TOKEN)
    with pytest.raises(IceSignalingError, match="verified grant"):
        await channel(
            TOKEN,
            json.dumps({"v": 1, "op": "ice.begin", "attempt_id": ATTEMPT}).encode(),
        )
    await channel.aclose()


def test_ice_grant_handler_requires_publisher():
    key, cert = viewer_credentials()
    with pytest.raises(ValueError, match="requires a Publisher"):
        link_serving.make_ice_grant_handler(
            "test-org",
            configuration_provider=lambda token, policy: configuration(),
            key=key,
            channel_cert=cert,
            peer_runtime=object(),
            signaling_capacity=IceCapacity(4, per_token_limit=2),
            modules=object(),
        )


def test_turn_control_reply_becomes_the_existing_ice_configuration():
    expected = configuration()
    reply = {
        "id": "c" * 32,
        "ok": True,
        "ice_servers": list(expected.ice_servers),
        "expires_at": expected.expires_at,
    }

    assert link_serving._turn_configuration_from_control(reply) == expected


@pytest.mark.parametrize(
    "reply",
    [
        {"ok": False, "error": "unavailable"},
        {"ok": True, "ice_servers": "not-a-list", "expires_at": 123},
        {"ok": True, "ice_servers": ["not-an-object"], "expires_at": 123},
    ],
)
def test_turn_control_refusal_or_malformed_reply_fails_the_ice_attempt(reply):
    with pytest.raises(ConnectionError):
        link_serving._turn_configuration_from_control(reply)


@pytest.mark.asyncio
async def test_production_connector_routes_ice_begin_to_live_turn_issuance(monkeypatch):
    from tools.network.relaykit import aiortc_responder

    monkeypatch.setattr(
        link_serving,
        "check_grant",
        lambda token, **kwargs: {"token": token, "meta": {"ice_policy": "relay_only"}},
    )
    monkeypatch.setattr(aiortc_responder, "load_aiortc_modules", lambda: object())
    expected = configuration()

    class FakeConnector:
        def __init__(self, *args, **kwargs):
            self.handler = args[4]
            self.control_calls = []

        async def control(self, op, args):
            self.control_calls.append((op, args))
            return {
                "id": "c" * 32,
                "ok": True,
                "ice_servers": list(expected.ice_servers),
                "expires_at": expected.expires_at,
            }

    key, cert = viewer_credentials()
    connector = link_serving._make_ice_serving_connector(
        "wss://relay.test",
        "test-org",
        key,
        cert,
        cert,
        "test-org",
        Publisher(),
        min_backoff=0.2,
        max_backoff=5.0,
        connector_factory=FakeConnector,
    )

    channel = connector.handler.for_channel(TOKEN)
    reply = json.loads(await channel(
        TOKEN,
        json.dumps({"v": 1, "op": "ice.begin", "attempt_id": ATTEMPT}).encode(),
    ))
    assert connector.control_calls == [("issue-turn", {})]
    assert reply["policy"] == "relay_only"
    assert reply["ice_servers"] == list(expected.ice_servers)
    await channel.aclose()


@pytest.mark.asyncio
async def test_production_ice_connector_leaves_ordinary_application_messages_unchanged(
    monkeypatch,
):
    async def application_handler(token, raw):
        return b"application:" + token.encode() + b":" + raw

    monkeypatch.setattr(
        link_serving, "make_grant_handler", lambda *args, **kwargs: application_handler
    )

    class FakeConnector:
        def __init__(self, *args, **kwargs):
            self.handler = args[4]
            self.control_calls = []

        async def control(self, op, args):
            self.control_calls.append((op, args))
            raise AssertionError("ordinary application traffic requested TURN")

    key, cert = viewer_credentials()
    connector = link_serving._make_ice_serving_connector(
        "wss://relay.test",
        "test-org",
        key,
        cert,
        cert,
        "test-org",
        Publisher(),
        min_backoff=0.2,
        max_backoff=5.0,
        connector_factory=FakeConnector,
    )

    channel = connector.handler.for_channel(TOKEN)
    assert await channel(TOKEN, b'{"v":1,"op":"fetch"}') == (
        b"application:" + TOKEN.encode() + b':{"v":1,"op":"fetch"}'
    )
    assert connector.control_calls == []
    await channel.aclose()
