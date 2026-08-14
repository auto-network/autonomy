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
