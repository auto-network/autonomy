from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("aiortc")

from tools.network.idkit import Subject, issue_cert
from tools.network.relaykit import aiortc_responder
from tools.network.relaykit.aiortc_responder import (
    AIOICE_VERSION,
    AIORTC_VERSION,
    MAX_DATA_CHANNEL_MESSAGE_BYTES,
    AiortcResponder,
    AiortcResponderFactory,
    DATA_CHANNEL_QUEUE_RECORDS,
    PeerRuntime,
    _aiortc_configuration,
    _assert_relay_selected,
    _candidate_wire,
    _ice_connection,
    _pin_gathering,
    load_aiortc_modules,
)
from tools.network.relaykit.ice_signaling import (
    IceConfiguration,
    IceOffer,
    IceSignalingError,
    STUN_URL,
    TURN_URLS,
    assert_candidate_free_sdp,
    strip_candidate_lines,
)

from .conftest import ORG, TOKEN


def configuration():
    return IceConfiguration(
        ice_servers=(
            {"urls": [STUN_URL]},
            {
                "urls": list(TURN_URLS),
                "username": "4000000000:test",
                "credential": "synthetic-test-credential",
                "credentialType": "password",
            },
        ),
        expires_at=4_000_000_000,
    )


def neutral_viewer_cert(root, key, now):
    return issue_cert(
        root,
        key.public_hex,
        scope=("tunnel:serve",),
        org=ORG,
        subject=Subject("operator", key.public_hex),
        not_before=now - 5,
        not_after=now + 3600,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("policy", "expected_addresses", "expected_policy"),
    [
        ("direct_allowed", ["192.0.2.10"], "ALL"),
        ("relay_only", [], "RELAY"),
    ],
)
async def test_pinned_gather_wrapper_bounds_time_and_removes_all_local_addresses(
    policy, expected_addresses, expected_policy
):
    modules = load_aiortc_modules()
    assert AIORTC_VERSION == "1.15.0"
    assert AIOICE_VERSION == "0.10.2"
    peer = modules.RTCPeerConnection()
    peer.createDataChannel("autonomy-v1")
    _ice, _gatherer, connection = _ice_connection(peer, modules)
    calls = []

    async def original(*, component, addresses, timeout):
        calls.append((component, addresses, timeout))
        return []

    connection.get_component_candidates = original
    _pin_gathering(peer, policy, modules)
    await connection.get_component_candidates(
        component=1, addresses=["192.0.2.10"]
    )
    assert calls == [(1, expected_addresses, 2)]
    assert connection._transport_policy.name == expected_policy
    await peer.close()


def test_selected_pair_check_rejects_any_nonrelay_local_path(monkeypatch):
    connection = SimpleNamespace(
        _nominated={1: SimpleNamespace(local_candidate=SimpleNamespace(type="relay"))}
    )
    monkeypatch.setattr(
        aiortc_responder,
        "_ice_connection",
        lambda peer, modules: (object(), object(), connection),
    )
    _assert_relay_selected(object(), object())
    connection._nominated[2] = SimpleNamespace(
        local_candidate=SimpleNamespace(type="srflx")
    )
    with pytest.raises(aiortc_responder.AiortcRuntimeError, match="direct local path"):
        _assert_relay_selected(object(), object())


def test_selected_pair_check_rejects_no_nominated_path(monkeypatch):
    connection = SimpleNamespace(_nominated={})
    monkeypatch.setattr(
        aiortc_responder,
        "_ice_connection",
        lambda peer, modules: (object(), object(), connection),
    )
    with pytest.raises(aiortc_responder.AiortcRuntimeError, match="direct local path"):
        _assert_relay_selected(object(), object())


def test_candidate_conversion_drops_hosts_and_scrubs_related_addresses():
    modules = load_aiortc_modules()
    host = modules.RTCIceCandidate(
        component=1,
        foundation="host",
        ip="192.168.1.3",
        port=5000,
        priority=1,
        protocol="udp",
        type="host",
    )
    assert _candidate_wire(
        host, mid="0", ufrag="u", policy="direct_allowed", modules=modules
    ) is None

    srflx = modules.RTCIceCandidate(
        component=1,
        foundation="srflx",
        ip="1.1.1.1",
        port=5001,
        priority=1,
        protocol="udp",
        type="srflx",
        relatedAddress="192.168.1.3",
        relatedPort=5000,
    )
    value = _candidate_wire(
        srflx, mid="0", ufrag="u", policy="direct_allowed", modules=modules
    )
    assert "raddr 0.0.0.0 rport 9" in value["candidate"]
    assert "192.168.1.3" not in value["candidate"]

    relay = modules.RTCIceCandidate(
        component=1,
        foundation="relay",
        ip="8.8.8.8",
        port=5002,
        priority=1,
        protocol="udp",
        type="relay",
        relatedAddress="1.1.1.1",
        relatedPort=5000,
    )
    value = _candidate_wire(
        relay, mid="0", ufrag="u", policy="relay_only", modules=modules
    )
    assert "raddr 0.0.0.0 rport 9" in value["candidate"]
    assert "1.1.1.1" not in value["candidate"]


def test_peer_runtime_reserves_before_answer_and_transfers_by_object_identity():
    runtime = PeerRuntime(2, per_token_limit=1)
    first = runtime.reserve(TOKEN)
    with pytest.raises(IceSignalingError, match="capacity"):
        runtime.reserve(TOKEN)
    responder = object()
    runtime.adopt(responder, first)
    assert runtime.active == 1
    other = object()
    with pytest.raises(RuntimeError, match="invalid"):
        runtime.adopt(other, first)
    runtime.release(responder, first)
    assert runtime.active == 0


def test_peer_runtime_releases_a_slot_when_no_responder_was_constructed():
    runtime = PeerRuntime(2, per_token_limit=1)
    reservation = runtime.reserve(TOKEN)
    runtime.release_reservation(reservation)
    assert runtime.active == 0
    replacement = runtime.reserve(TOKEN)
    runtime.release_reservation(replacement)
    assert runtime.active == 0


@pytest.mark.asyncio
async def test_factory_constructor_failure_cannot_leak_a_reserved_peer(
    monkeypatch, root, session_key, now
):
    runtime = PeerRuntime(2, per_token_limit=1)
    real_modules = load_aiortc_modules()
    peers = []

    def tracked_peer(*args, **kwargs):
        peer = real_modules.RTCPeerConnection(*args, **kwargs)
        peers.append(peer)
        return peer

    modules = SimpleNamespace(
        **{
            **real_modules.__dict__,
            "RTCPeerConnection": tracked_peer,
        }
    )

    class ConstructorFailure:
        def __init__(self, **kwargs):
            raise RuntimeError("synthetic constructor failure")

    monkeypatch.setattr(aiortc_responder, "AiortcResponder", ConstructorFailure)
    factory = AiortcResponderFactory(
        token=TOKEN,
        owner=runtime,
        key=session_key,
        cert=neutral_viewer_cert(root, session_key, now),
        org=ORG,
        application_handler=lambda token, message: message,
        modules=modules,
    )
    with pytest.raises(RuntimeError, match="constructor failure"):
        await factory(configuration(), "direct_allowed")
    assert runtime.active == 0
    assert len(peers) == 1
    assert peers[0].connectionState == "closed"


def test_factory_refuses_an_identity_bearing_viewer_certificate(
    session_key, session_cert
):
    """xw5ow: caller miswiring must not put identity in SERVER_HELLO."""
    runtime = PeerRuntime(2, per_token_limit=1)
    with pytest.raises(aiortc_responder.AiortcRuntimeError, match="identity-neutral"):
        AiortcResponderFactory(
            token=TOKEN,
            owner=runtime,
            key=session_key,
            cert=session_cert,
            org=ORG,
            application_handler=lambda token, message: message,
            modules=load_aiortc_modules(),
        )
    assert runtime.active == 0


@pytest.mark.asyncio
async def test_close_waits_for_owned_background_tasks_and_releases_peer(
    session_key, session_cert
):
    modules = load_aiortc_modules()
    runtime = PeerRuntime(2, per_token_limit=1)
    reservation = runtime.reserve(TOKEN)
    responder = AiortcResponder(
        token=TOKEN,
        policy="direct_allowed",
        owner=runtime,
        reservation=reservation,
        peer=modules.RTCPeerConnection(
            _aiortc_configuration(configuration(), modules)
        ),
        key=session_key,
        cert=session_cert,
        org=ORG,
        application_handler=lambda token, message: message,
        modules=modules,
    )
    runtime.adopt(responder, reservation)
    responder._transferred = True
    responder._watchdog = asyncio.create_task(asyncio.Event().wait())
    responder._channel_task = asyncio.create_task(asyncio.Event().wait())
    tasks = (responder._watchdog, responder._channel_task)
    await responder.aclose()
    assert all(task.done() for task in tasks)
    assert runtime.active == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_message",
    ["text is forbidden", b"x" * (MAX_DATA_CHANNEL_MESSAGE_BYTES + 1)],
)
async def test_datachannel_refuses_unbounded_or_nonbinary_input_before_buffering(
    monkeypatch, session_key, session_cert, bad_message
):
    modules = load_aiortc_modules()
    runtime = PeerRuntime(2, per_token_limit=1)
    reservation = runtime.reserve(TOKEN)
    responder = AiortcResponder(
        token=TOKEN,
        policy="direct_allowed",
        owner=runtime,
        reservation=reservation,
        peer=modules.RTCPeerConnection(
            _aiortc_configuration(configuration(), modules)
        ),
        key=session_key,
        cert=session_cert,
        org=ORG,
        application_handler=lambda token, message: message,
        modules=modules,
    )

    async def held_channel(**kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(aiortc_responder, "serve_channel", held_channel)

    class Channel:
        label = "autonomy-v1"
        readyState = "open"
        bufferedAmount = 0

        def __init__(self):
            self.callbacks = {}

        def on(self, name):
            def register(callback):
                self.callbacks[name] = callback
                return callback
            return register

        def send(self, payload):  # pragma: no cover - held_channel never sends
            raise AssertionError("unexpected send")

    channel = Channel()
    task = asyncio.create_task(responder._serve_datachannel(channel))
    await asyncio.sleep(0)
    channel.callbacks["message"](bad_message)
    for _ in range(10):
        if responder._closed:
            break
        await asyncio.sleep(0)
    assert responder._closed
    await asyncio.gather(task, return_exceptions=True)
    assert runtime.active == 0


@pytest.mark.asyncio
async def test_datachannel_input_queue_is_bounded(monkeypatch, session_key, session_cert):
    modules = load_aiortc_modules()
    runtime = PeerRuntime(2, per_token_limit=1)
    reservation = runtime.reserve(TOKEN)
    responder = AiortcResponder(
        token=TOKEN,
        policy="direct_allowed",
        owner=runtime,
        reservation=reservation,
        peer=modules.RTCPeerConnection(
            _aiortc_configuration(configuration(), modules)
        ),
        key=session_key,
        cert=session_cert,
        org=ORG,
        application_handler=lambda token, message: message,
        modules=modules,
    )

    async def held_channel(**kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(aiortc_responder, "serve_channel", held_channel)

    class Channel:
        label = "autonomy-v1"
        readyState = "open"
        bufferedAmount = 0

        def __init__(self):
            self.callbacks = {}

        def on(self, name):
            def register(callback):
                self.callbacks[name] = callback
                return callback
            return register

    channel = Channel()
    task = asyncio.create_task(responder._serve_datachannel(channel))
    await asyncio.sleep(0)
    for _ in range(DATA_CHANNEL_QUEUE_RECORDS + 1):
        channel.callbacks["message"](b"bounded-record")
    for _ in range(10):
        if responder._closed:
            break
        await asyncio.sleep(0)
    assert responder._closed
    await asyncio.gather(task, return_exceptions=True)
    assert runtime.active == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("authorized", [True, False])
async def test_fresh_application_handshake_rechecks_the_local_grant(
    monkeypatch, session_key, session_cert, authorized
):
    modules = load_aiortc_modules()
    runtime = PeerRuntime(2, per_token_limit=1)
    reservation = runtime.reserve(TOKEN)
    application_calls = []

    async def application_handler(token, message):
        application_calls.append((token, message))
        return b"response"

    responder = AiortcResponder(
        token=TOKEN,
        policy="direct_allowed",
        owner=runtime,
        reservation=reservation,
        peer=modules.RTCPeerConnection(
            _aiortc_configuration(configuration(), modules)
        ),
        key=session_key,
        cert=session_cert,
        org=ORG,
        application_handler=application_handler,
        authorization_check=lambda token: authorized,
        modules=modules,
    )

    async def exercise_handler(_key, _cert, **kwargs):
        return await kwargs["handler"](TOKEN, b"first-application-request")

    monkeypatch.setattr(aiortc_responder, "serve_channel", exercise_handler)

    class Channel:
        bufferedAmountLowThreshold = 0
        readyState = "open"
        bufferedAmount = 0

        def on(self, _name):
            return lambda callback: callback

    if authorized:
        await responder._serve_datachannel(Channel())
        assert responder.established
        assert application_calls == [(TOKEN, b"first-application-request")]
    else:
        with pytest.raises(PermissionError, match="no longer valid"):
            await responder._serve_datachannel(Channel())
        assert not responder.established
        assert application_calls == []
    assert runtime.active == 0


@pytest.mark.asyncio
async def test_real_aiortc_answer_has_no_candidate_or_connection_address_leak(
    session_key, session_cert
):
    modules = load_aiortc_modules()
    viewer = modules.RTCPeerConnection()
    viewer.createDataChannel("autonomy-v1")
    offer = await viewer.createOffer()
    # createOffer before setLocalDescription contains the ICE/DTLS shape but
    # has not gathered any candidates: exactly the separately-carried form.
    clean_offer = strip_candidate_lines(offer.sdp)
    assert assert_candidate_free_sdp(clean_offer) == clean_offer

    owner = PeerRuntime(2, per_token_limit=1)
    reservation = owner.reserve(TOKEN)

    async def application_handler(token, message):
        return message

    responder = AiortcResponder(
        token=TOKEN,
        policy="direct_allowed",
        owner=owner,
        reservation=reservation,
        peer=modules.RTCPeerConnection(
            _aiortc_configuration(configuration(), modules)
        ),
        key=session_key,
        cert=session_cert,
        org=ORG,
        application_handler=application_handler,
        modules=modules,
    )
    answer = await responder.answer(
        IceOffer(attempt_id="a" * 32, sdp=clean_offer, candidates=()),
        timeout=6.0,
    )
    wire = strip_candidate_lines(answer.sdp)
    assert assert_candidate_free_sdp(wire) == wire
    assert "a=candidate:" not in wire
    placeholders = {"0.0.0.0", "::"}
    address_positions = []
    for line in wire.splitlines():
        fields = line.split()
        if line.startswith("o=") or line.startswith("c=IN IP"):
            address_positions.append(fields[-1])
        elif line.startswith("a=rtcp:") and "IN" in fields:
            address_positions.append(fields[-1])
    for candidate in answer.candidates:
        fields = candidate["candidate"].split()
        for index, field in enumerate(fields[:-1]):
            if field.lower() == "raddr":
                address_positions.append(fields[index + 1])
    assert address_positions
    assert set(address_positions) <= placeholders
    await responder.aclose()
    await viewer.close()
    assert owner.active == 0
