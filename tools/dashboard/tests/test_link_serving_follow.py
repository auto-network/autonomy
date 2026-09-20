"""The ``follow`` op and the ``org:follow`` grant (design of record
graph://5f2f5a49-00d §10.1).

An ``org:follow`` link is a standing, membership-free public link a node
pulls the org's public surface from. Its ONLY accepted op is ``follow``,
served by the fleet-sync scheduler; every other op is the uniform refusal on
that grant, and ``follow`` is the uniform refusal on every other grant. The
follow admission is built from the grant with no client credential — the
link's fragment key already authenticated the exchange — so an unarmed
process (no fleet scheduler) refuses a follow with a typed close, the way the
fleet stream offer handler refuses an offer.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from tools.dashboard import link_serving

ORG = "followorg"
ORG_UUID = "99999999-9999-4999-8999-999999999999"
FOLLOW_TOKEN = "aa" * 32
NOTE_TOKEN = "bb" * 32
MISSION_TOKEN = "cc" * 32
FLEET_JOIN_TOKEN = "dd" * 32
TARGET_UUID = "11111111-1111-4111-8111-111111111111"

# A minimal fleet-sync pull request object the follow envelope carries. The
# stub scheduler ignores its content; a real scheduler decodes it.
PULL = {
    "v": 3, "op": "pull",
    "roster_epoch": "00" * 32, "compat": "11" * 32, "resume": [],
    "scope": "autonomy",
}


class StubScheduler:
    """A scheduler stand-in whose ``_handle`` records its admission and
    returns a two-frame async iterator — the shape a real sweep/delta reply
    has. Proves the follow op reaches the scheduler and streams its reply."""

    def __init__(self):
        self.calls = []

    async def _handle(self, token, message, peer_pub, *, admission=None,
                      telemetry_channel=None, **kw):
        self.calls.append(
            {"token": token, "message": message, "peer_pub": peer_pub,
             "admission": admission, "telemetry_channel": telemetry_channel}
        )

        async def _stream():
            yield b"sweep-frame-1"
            yield b"sweep-frame-2"

        return _stream()


class StubRuntime:
    def __init__(self, scheduler):
        self.scheduler = scheduler


@pytest.fixture
def grants(monkeypatch):
    rows = {
        FOLLOW_TOKEN: {
            "token": FOLLOW_TOKEN, "target_type": "org:follow",
            "target_uuid": ORG_UUID,
            "meta": {"org": ORG, "org_uuid": ORG_UUID},
        },
        NOTE_TOKEN: {
            "token": NOTE_TOKEN, "target_type": "note",
            "target_uuid": TARGET_UUID, "meta": {},
        },
        MISSION_TOKEN: {
            "token": MISSION_TOKEN, "target_type": "mission",
            "target_uuid": TARGET_UUID, "meta": {"participant_id": "guest:a"},
        },
        FLEET_JOIN_TOKEN: {
            "token": FLEET_JOIN_TOKEN, "target_type": "fleet:join",
            "target_uuid": TARGET_UUID, "meta": {},
        },
    }
    monkeypatch.setattr(
        link_serving, "check_grant",
        lambda token, org=None, now=None, grant_id=None: rows.get(token),
    )
    return rows


def _collect(reply):
    """Reply → (bytes) for a synchronous refusal, or [frames] for a stream."""
    if reply is None or isinstance(reply, (bytes, bytearray)):
        return reply

    async def _drain():
        return [frame async for frame in reply]

    return asyncio.run(_drain())


def _call(token, request, *, fleet_runtime=None):
    async def _run():
        handler = link_serving.make_grant_handler(
            ORG, fleet_runtime=fleet_runtime,
        )
        reply = await handler(token, json.dumps(request).encode())
        if reply is None or isinstance(reply, (bytes, bytearray)):
            return reply
        return [frame async for frame in reply]

    return asyncio.run(_run())


# ── follow accepted on an org:follow grant ───────────────────────


def test_follow_on_org_follow_grant_streams_the_scheduler_reply(grants):
    sched = StubScheduler()
    frames = _call(
        FOLLOW_TOKEN, {"v": 1, "op": "follow", "request": PULL},
        fleet_runtime=StubRuntime(sched),
    )
    assert frames == [b"sweep-frame-1", b"sweep-frame-2"]
    assert len(sched.calls) == 1
    call = sched.calls[0]
    # No peer credential; a follow admission carrying kind + the org uuid.
    assert call["peer_pub"] == ""
    assert call["admission"].kind == "follow"
    assert call["admission"].org == ORG_UUID
    # The nested pull request is what crossed to the scheduler.
    assert json.loads(call["message"]) == PULL


def test_follow_admission_cannot_be_obtained_by_a_fleet_join_grant(grants):
    """A fleet:join grant never yields a follow admission — it is refused
    before any scheduler call."""
    sched = StubScheduler()
    reply = _call(
        FLEET_JOIN_TOKEN, {"v": 1, "op": "follow", "request": PULL},
        fleet_runtime=StubRuntime(sched),
    )
    assert reply == link_serving.REFUSED
    assert sched.calls == []


@pytest.mark.parametrize("token", [NOTE_TOKEN, MISSION_TOKEN, FLEET_JOIN_TOKEN])
def test_follow_refused_on_every_other_grant(grants, token):
    sched = StubScheduler()
    reply = _call(
        token, {"v": 1, "op": "follow", "request": PULL},
        fleet_runtime=StubRuntime(sched),
    )
    assert reply == link_serving.REFUSED
    assert sched.calls == []


def test_follow_refused_on_an_unknown_token(grants):
    sched = StubScheduler()
    reply = _call(
        "ee" * 32, {"v": 1, "op": "follow", "request": PULL},
        fleet_runtime=StubRuntime(sched),
    )
    assert reply == link_serving.REFUSED
    assert sched.calls == []


# ── the unarmed refusal (typed close, not the uniform refusal) ────


def test_follow_on_unarmed_process_raises_a_typed_close(grants):
    """No fleet runtime armed → PermissionError (credential unavailable),
    which the connector maps to CLOSE_CONNECTOR_UNARMED — the same posture
    the offer handler takes, NOT the uniform content refusal."""
    with pytest.raises(PermissionError, match="unarmed"):
        _call(FOLLOW_TOKEN, {"v": 1, "op": "follow", "request": PULL},
              fleet_runtime=None)


def test_follow_when_runtime_has_no_scheduler_raises_a_typed_close(grants):
    with pytest.raises(PermissionError, match="unarmed"):
        _call(FOLLOW_TOKEN, {"v": 1, "op": "follow", "request": PULL},
              fleet_runtime=StubRuntime(None))


def test_follow_without_a_nested_request_is_a_bad_request(grants):
    sched = StubScheduler()
    reply = _call(
        FOLLOW_TOKEN, {"v": 1, "op": "follow"},
        fleet_runtime=StubRuntime(sched),
    )
    assert reply == link_serving.BAD_REQUEST
    assert sched.calls == []


# ── every other op refused on an org:follow grant ────────────────


def test_fetch_on_org_follow_grant_is_the_uniform_refusal(grants):
    # An org:follow grant resolves to no artifact, so the content fetch path
    # refuses it uniformly.
    assert _call(FOLLOW_TOKEN, {"v": 1, "op": "fetch"}) == link_serving.REFUSED


def test_read_on_org_follow_grant_is_refused(grants):
    assert _call(
        FOLLOW_TOKEN, {"v": 1, "op": "read", "body": {}}
    ) == link_serving.REFUSED


def test_write_on_org_follow_grant_is_refused(grants):
    assert _call(
        FOLLOW_TOKEN, {"v": 1, "op": "write", "body": {}}
    ) == link_serving.REFUSED


def test_join_on_org_follow_grant_is_refused(grants):
    assert _call(
        FOLLOW_TOKEN, {"v": 1, "op": "context"}
    ) == link_serving.REFUSED


def test_fleet_join_on_org_follow_grant_is_refused(grants):
    assert _call(
        FOLLOW_TOKEN, {"v": 1, "op": "fleet.request"}
    ) == link_serving.REFUSED


def test_subscribe_on_org_follow_grant_is_refused(grants):
    assert _call(
        FOLLOW_TOKEN, {"v": 1, "op": "subscribe"}
    ) == link_serving.REFUSED


def test_attachment_fetch_on_org_follow_grant_is_not_authorized(grants):
    frames = _call(
        FOLLOW_TOKEN,
        {"v": 1, "op": "attachment.fetch", "ref": "some-ref", "offset": 0},
    )
    assert isinstance(frames, list) and frames
    msg = json.loads(frames[0])
    assert msg["op"] == "error" and msg["code"] == "not_authorized"
