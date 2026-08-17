from __future__ import annotations

import json
import asyncio

import pytest

from tools.network.relaykit.ice_signaling import (
    IceAnswer,
    IceCapacity,
    IceConfiguration,
    IceSignalingError,
    IceSignalingSession,
    MAX_ATTEMPT_SIGNAL_BYTES,
    STUN_URL,
    TURN_URLS,
    assert_candidate_free_sdp,
    parse_message,
    strip_candidate_lines,
    validate_begin,
    validate_candidate,
    validate_candidates,
    validate_offer,
)


ATTEMPT = "ab" * 16


def candidate(line, *, mid="0", index=0, ufrag="ufrag"):
    return {
        "candidate": line,
        "sdpMid": mid,
        "sdpMLineIndex": index,
        "usernameFragment": ufrag,
    }


def configuration(expires_at=2000):
    return IceConfiguration(
        ice_servers=(
            {"urls": [STUN_URL]},
            {
                "urls": list(TURN_URLS),
                "username": "2000:synthetic-test-user",
                "credential": "synthetic-test-credential",
                "credentialType": "password",
            },
        ),
        expires_at=expires_at,
    )


def test_begin_has_one_exact_versioned_shape():
    raw = json.dumps({"v": 1, "op": "ice.begin", "attempt_id": ATTEMPT}).encode()
    value, size = parse_message(raw)
    assert validate_begin(value, size) == ATTEMPT
    for bad in (
        {**value, "policy": "relay_only"},
        {**value, "v": True},
        {**value, "attempt_id": ATTEMPT.upper()},
    ):
        with pytest.raises(IceSignalingError):
            validate_begin(bad, len(json.dumps(bad)))


def test_duplicate_json_key_is_refused_before_interpretation():
    with pytest.raises(IceSignalingError, match="duplicate"):
        parse_message(b'{"v":1,"v":2,"op":"ice.begin","attempt_id":"' +
                      ATTEMPT.encode() + b'"}')


def test_pathologically_deep_json_is_a_typed_signaling_error():
    raw = (b'{"v":' + b'[' * 10_000 + b'0' + b']' * 10_000 + b'}')
    with pytest.raises(IceSignalingError, match="valid JSON"):
        parse_message(raw)


def test_public_direct_candidates_allow_mdns_reflexive_and_relay():
    lines = [
        "candidate:1 1 udp 2122260223 host-a.local 50000 typ host generation 0",
        "candidate:2 1 udp 1686052607 203.0.113.10 40000 typ srflx "
        "raddr 0.0.0.0 rport 9 generation 0",
        "candidate:3 1 udp 1677734911 8.8.8.8 49160 typ relay "
        "raddr 0.0.0.0 rport 9 generation 0",
    ]
    # 203.0.113.0/24 is documentation space and ipaddress correctly refuses
    # it as non-global; use an actually global address for the positive case.
    lines[1] = lines[1].replace("203.0.113.10", "1.1.1.1")
    assert len(validate_candidates(
        [candidate(line) for line in lines], "direct_allowed"
    )) == 3


@pytest.mark.parametrize(
    "line",
    [
        "candidate:1 1 udp 1 192.168.1.2 50000 typ host",
        "candidate:1 1 udp 1 10.0.0.2 50000 typ srflx raddr 10.0.0.1 rport 50001",
        "candidate:1 1 udp 1 169.254.169.254 50000 typ relay raddr 0.0.0.0 rport 9",
        "candidate:1 1 udp 1 ::ffff:169.254.169.254 50000 typ relay raddr :: rport 9",
    ],
)
def test_private_literal_and_metadata_candidates_are_refused(line):
    with pytest.raises(IceSignalingError):
        validate_candidate(candidate(line), "direct_allowed")


def test_related_private_address_must_use_the_standard_privacy_representation():
    leaked = candidate(
        "candidate:2 1 udp 1 1.1.1.1 40000 typ srflx "
        "raddr 192.168.1.2 rport 50000"
    )
    with pytest.raises(IceSignalingError, match="privacy-scrubbed"):
        validate_candidate(leaked, "direct_allowed")

    scrubbed = {**leaked, "candidate": leaked["candidate"].replace(
        "192.168.1.2 rport 50000", "0.0.0.0 rport 9"
    )}
    assert validate_candidate(scrubbed, "direct_allowed") == scrubbed


def test_relay_only_refuses_every_direct_candidate():
    host = candidate("candidate:1 1 udp 1 host-a.local 50000 typ host")
    srflx = candidate(
        "candidate:2 1 udp 1 1.1.1.1 40000 typ srflx raddr 0.0.0.0 rport 9"
    )
    for value in (host, srflx):
        with pytest.raises(IceSignalingError, match="relay-only"):
            validate_candidate(value, "relay_only")

    relay = candidate(
        "candidate:3 1 udp 1 8.8.8.8 49160 typ relay raddr 0.0.0.0 rport 9"
    )
    assert validate_candidate(relay, "relay_only") == relay


def test_candidate_object_field_and_count_bounds_are_exact():
    base = candidate("candidate:1 1 udp 1 host-a.local 50000 typ host")
    with pytest.raises(IceSignalingError, match="field set"):
        validate_candidate({**base, "address": "192.168.1.2"}, "direct_allowed")
    with pytest.raises(IceSignalingError, match="array"):
        validate_candidates([base] * 33, "direct_allowed")
    with pytest.raises(IceSignalingError, match="byte limit"):
        validate_candidate({**base, "candidate": "x" * 2049}, "direct_allowed")


def test_offer_refuses_candidate_smuggling_in_sdp_and_cross_attempt_data():
    offer = {
        "v": 1,
        "op": "ice.offer",
        "attempt_id": ATTEMPT,
        "sdp": "v=0\r\na=candidate:1 1 udp 1 192.168.1.2 50000 typ host\r\n",
        "candidates": [],
    }
    raw = json.dumps(offer).encode()
    value, size = parse_message(raw)
    with pytest.raises(IceSignalingError, match="smuggled"):
        validate_offer(
            value, size, attempt_id=ATTEMPT, policy="direct_allowed"
        )
    offer["sdp"] = "v=0\r\n"
    with pytest.raises(IceSignalingError, match="match"):
        validate_offer(
            offer, len(json.dumps(offer)), attempt_id="cd" * 16,
            policy="direct_allowed",
        )


def test_outgoing_sdp_is_stripped_then_asserted_candidate_free():
    dirty = (
        "v=0\r\n"
        "c=IN IP4 192.168.1.2\r\n"
        "a=group:BUNDLE 0\r\n"
        "a=candidate:1 1 udp 1 192.168.1.2 50000 typ host\r\n"
        "a=end-of-candidates\r\n"
    )
    clean = strip_candidate_lines(dirty)
    assert "a=candidate:" not in clean
    assert "c=IN IP4 0.0.0.0\r\n" in clean
    assert "192.168.1.2" not in clean
    assert "a=end-of-candidates" not in clean
    assert assert_candidate_free_sdp(clean) == clean
    with pytest.raises(IceSignalingError, match="smuggled"):
        assert_candidate_free_sdp("v=0\r\na=end-of-candidates\r\n")


def test_incoming_sdp_refuses_address_smuggling_outside_candidates():
    with pytest.raises(IceSignalingError, match="connection address"):
        assert_candidate_free_sdp("v=0\r\nc=IN IP6 fd00::1\r\n")
    with pytest.raises(IceSignalingError, match="connection address"):
        assert_candidate_free_sdp("v=0\r\n  c=IN IP4 192.168.1.2  \r\n")
    assert assert_candidate_free_sdp(
        "v=0\r\nc=IN IP4 0.0.0.0\r\nc=IN IP6 ::\r\n"
    )


def test_total_attempt_byte_budget_includes_begin_and_offer():
    offer = {
        "v": 1,
        "op": "ice.offer",
        "attempt_id": ATTEMPT,
        "sdp": "v=0\r\n",
        "candidates": [],
    }
    wire = len(json.dumps(offer))
    with pytest.raises(IceSignalingError, match="byte limit"):
        validate_offer(
            offer,
            wire,
            attempt_id=ATTEMPT,
            policy="direct_allowed",
            bytes_already_used=MAX_ATTEMPT_SIGNAL_BYTES - wire + 1,
        )


@pytest.mark.asyncio
async def test_session_transfers_only_after_terminal_answer_is_confirmed_sent():
    class Adapter:
        def __init__(self):
            self.closed = False
            self.transferred = False
            self.seen = None

        async def answer(self, offer, *, timeout):
            self.seen = (offer, timeout)
            return IceAnswer(
                sdp="v=0\r\na=candidate:9 1 udp 1 192.168.1.4 50000 typ host\r\n",
                candidates=(candidate(
                    "candidate:3 1 udp 1 8.8.8.8 49160 typ relay "
                    "raddr 0.0.0.0 rport 9"
                ),),
            )

        async def aclose(self):
            self.closed = True

        def transfer(self):
            self.transferred = True

    adapter = Adapter()
    capacity = IceCapacity(4, per_token_limit=2)
    session = IceSignalingSession(
        token="a" * 32,
        policy="direct_allowed",
        configuration_provider=lambda token, policy: configuration(),
        responder_factory=lambda config, policy: adapter,
        capacity=capacity,
        monotonic=lambda: 10.0,
        wall_clock=lambda: 1000,
    )
    config_reply = json.loads(await session(
        "a" * 32,
        json.dumps({"v": 1, "op": "ice.begin", "attempt_id": ATTEMPT}).encode(),
    ))
    assert config_reply == {
        "v": 1,
        "op": "ice.config",
        "attempt_id": ATTEMPT,
        "policy": "direct_allowed",
        "ice_servers": list(configuration().ice_servers),
        "expires_at": 2000,
    }
    assert capacity.active == 1
    assert session.on_response_sent() is False

    answer_reply = json.loads(await session(
        "a" * 32,
        json.dumps({
            "v": 1,
            "op": "ice.offer",
            "attempt_id": ATTEMPT,
            "sdp": "v=0\r\n",
            "candidates": [],
        }).encode(),
    ))
    assert answer_reply["op"] == "ice.answer"
    assert "a=candidate:" not in answer_reply["sdp"]
    assert len(answer_reply["candidates"]) == 1
    assert adapter.transferred is False
    assert capacity.active == 1
    # Ownership transfers after the terminal answer is sent, but the browser
    # owns normal signaling teardown so Relay cannot discard queued DATA by
    # immediately processing a following CLOSE.
    assert session.on_response_sent() is False
    assert adapter.transferred is True
    assert capacity.active == 0
    with pytest.raises(IceSignalingError, match="complete"):
        await session("a" * 32, json.dumps({
            "v": 1, "op": "ice.offer", "attempt_id": ATTEMPT,
            "sdp": "v=0\r\n", "candidates": [],
        }).encode())

    await session.aclose()
    assert adapter.closed is False
    assert capacity.active == 0


@pytest.mark.asyncio
async def test_unconfirmed_answer_remains_session_owned_and_is_closed():
    class Adapter:
        def __init__(self):
            self.closed = False
            self.transferred = False

        async def answer(self, offer, *, timeout):
            return IceAnswer(sdp="v=0\r\n", candidates=())

        async def aclose(self):
            self.closed = True

        def transfer(self):
            self.transferred = True

    adapter = Adapter()
    capacity = IceCapacity(2, per_token_limit=1)
    session = IceSignalingSession(
        token="a" * 32,
        policy="direct_allowed",
        configuration_provider=lambda token, policy: configuration(),
        responder_factory=lambda config, policy: adapter,
        capacity=capacity,
        monotonic=lambda: 10.0,
        wall_clock=lambda: 1000,
    )
    await session(
        "a" * 32,
        json.dumps({"v": 1, "op": "ice.begin", "attempt_id": ATTEMPT}).encode(),
    )
    assert session.on_response_sent() is False
    await session(
        "a" * 32,
        json.dumps({
            "v": 1, "op": "ice.offer", "attempt_id": ATTEMPT,
            "sdp": "v=0\r\n", "candidates": [],
        }).encode(),
    )
    # This models transport failure or cancellation before the connector's
    # post-send confirmation hook runs.
    await session.aclose()
    assert adapter.transferred is False
    assert adapter.closed is True
    assert capacity.active == 0


@pytest.mark.asyncio
async def test_capacity_exhaustion_refuses_upgrade_without_affecting_other_session():
    capacity = IceCapacity(3, per_token_limit=1)
    first = IceSignalingSession(
        token="a" * 32,
        policy="direct_allowed",
        configuration_provider=lambda token, policy: configuration(),
        responder_factory=lambda config, policy: None,
        capacity=capacity,
        monotonic=lambda: 10.0,
        wall_clock=lambda: 1000,
    )
    second = IceSignalingSession(
        token="a" * 32,
        policy="direct_allowed",
        configuration_provider=lambda token, policy: configuration(),
        responder_factory=lambda config, policy: None,
        capacity=capacity,
        monotonic=lambda: 10.0,
        wall_clock=lambda: 1000,
    )
    begin = json.dumps({"v": 1, "op": "ice.begin", "attempt_id": ATTEMPT}).encode()
    await first("a" * 32, begin)
    with pytest.raises(IceSignalingError, match="capacity"):
        await second("a" * 32, begin)
    assert capacity.active == 1

    other_token = "b" * 32
    other = IceSignalingSession(
        token=other_token,
        policy="direct_allowed",
        configuration_provider=lambda token, policy: configuration(),
        responder_factory=lambda config, policy: None,
        capacity=capacity,
        monotonic=lambda: 10.0,
        wall_clock=lambda: 1000,
    )
    await other(other_token, begin)
    assert capacity.active == 2
    await first.aclose()
    await second.aclose()
    await other.aclose()
    assert capacity.active == 0


@pytest.mark.asyncio
async def test_timeout_cancels_answer_and_teardown_releases_capacity():
    now = [0.0]

    class SlowAdapter:
        def __init__(self):
            self.closed = False

        async def answer(self, offer, *, timeout):
            await asyncio.sleep(timeout + 1)

        async def aclose(self):
            self.closed = True

    adapter = SlowAdapter()
    capacity = IceCapacity(2, per_token_limit=1)
    session = IceSignalingSession(
        token="a" * 32,
        policy="direct_allowed",
        configuration_provider=lambda token, policy: configuration(),
        responder_factory=lambda config, policy: adapter,
        capacity=capacity,
        monotonic=lambda: now[0],
        wall_clock=lambda: 1000,
        deadline_seconds=0.01,
    )
    await session(
        "a" * 32,
        json.dumps({"v": 1, "op": "ice.begin", "attempt_id": ATTEMPT}).encode(),
    )
    assert session.on_response_sent() is False
    with pytest.raises(asyncio.TimeoutError):
        await session(
            "a" * 32,
            json.dumps({
                "v": 1, "op": "ice.offer", "attempt_id": ATTEMPT,
                "sdp": "v=0\r\n", "candidates": [],
            }).encode(),
        )
    await session.aclose()
    assert adapter.closed is True
    assert capacity.active == 0


def test_config_rejects_expired_credentials_and_caller_selected_servers():
    from tools.network.relaykit.ice_signaling import validate_ice_configuration

    with pytest.raises(IceSignalingError, match="expired"):
        validate_ice_configuration(configuration(expires_at=999), now=1000)
    bad = configuration()
    bad_servers = list(bad.ice_servers)
    bad_servers[0] = {"urls": ["stun:attacker.example:3478"]}
    with pytest.raises(IceSignalingError, match="frozen service"):
        validate_ice_configuration(
            IceConfiguration(tuple(bad_servers), bad.expires_at), now=1000
        )
