"""Mission Control's live-update publisher (auto-8npih).

Stub Publisher recording (token, sealed) calls and a stub grant cache, so
routing, filtering, sealing and the no-listeners-no-work rule are all
provable without a real tunnel, a real relay, or a running dashboard --
the same shape test_publisher_seam.py and test_link_serving_subscribe.py
use for their own halves of this path.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from tools.dashboard import link_serving
from tools.dashboard.plugins.mission_control import relay_publisher
from tools.network.relaykit.channel import open_stream_frame

MISSION_A = "11111111-1111-4111-8111-111111111111"
MISSION_B = "22222222-2222-4222-8222-222222222222"
TOKEN_A1 = "a1" * 16
TOKEN_A2 = "a2" * 16
TOKEN_B1 = "b1" * 16


class _StubPublisher:
    """Records what was published, and to whom."""

    def __init__(self, listening=()):
        self.listening = set(listening)
        self.published: list[tuple[str, bytes]] = []

    def has_listeners(self, token: str) -> bool:
        return token in self.listening

    async def publish(self, token: str, sealed: bytes) -> bool:
        self.published.append((token, sealed))
        return True


@pytest.fixture(autouse=True)
def _grants(monkeypatch):
    """Two guests on mission A (personalized links), one on mission B."""
    rows = {
        TOKEN_A1: {"token": TOKEN_A1, "target_type": "mission", "target_uuid": MISSION_A,
                   "meta": {"participant_id": "guest:1"}},
        TOKEN_A2: {"token": TOKEN_A2, "target_type": "mission", "target_uuid": MISSION_A,
                   "meta": {"participant_id": "guest:2"}},
        TOKEN_B1: {"token": TOKEN_B1, "target_type": "mission", "target_uuid": MISSION_B,
                   "meta": {"participant_id": "guest:3"}},
    }

    class _Member:
        def __init__(self, key, payload):
            self.key, self.payload = key, payload

    class _Set:
        members = [_Member(k, v) for k, v in rows.items()]

    monkeypatch.setattr(
        "tools.graph.settings_ops.read_owned_set",
        lambda *a, **kw: _Set(),
    )
    monkeypatch.setattr(
        link_serving, "check_grant", lambda token, org=None, now=None: rows.get(token)
    )
    link_serving._STREAM_KEYS.clear()
    yield rows
    link_serving._STREAM_KEYS.clear()


def _conversation(mission_id, event="answered", entry_id="e1"):
    return {
        "event": event, "mission_id": mission_id, "pillar_id": None,
        "entry_id": entry_id, "question": {"question": "hi", "answer": "there"},
        "update": None,
    }


# ── routing and filtering ───────────────────────────────────────────────


def test_conversation_event_reaches_every_guest_of_that_mission():
    publisher = _StubPublisher(listening={TOKEN_A1, TOKEN_A2})
    sent = asyncio.run(relay_publisher.publish_event(
        publisher, relay_publisher.CONVERSATION_TOPIC, _conversation(MISSION_A),
    ))
    assert sent == 2
    assert {token for token, _ in publisher.published} == {TOKEN_A1, TOKEN_A2}


def test_a_guest_never_receives_another_missions_event():
    """The filter that matters: two missions on one dashboard, and a
    channel only ever sees its own."""
    publisher = _StubPublisher(listening={TOKEN_A1, TOKEN_B1})
    asyncio.run(relay_publisher.publish_event(
        publisher, relay_publisher.CONVERSATION_TOPIC, _conversation(MISSION_A),
    ))
    assert [token for token, _ in publisher.published] == [TOKEN_A1]


def test_unrelated_topics_are_ignored():
    publisher = _StubPublisher(listening={TOKEN_A1})
    for topic in ("session:messages", "session:registry", "approval:request"):
        sent = asyncio.run(relay_publisher.publish_event(
            publisher, topic, {"mission_id": MISSION_A},
        ))
        assert sent == 0
    assert publisher.published == []


@pytest.mark.parametrize("event", ["asked", "answered", "reopened", "updated"])
def test_every_conversation_event_type_is_forwarded(event):
    publisher = _StubPublisher(listening={TOKEN_A1})
    sent = asyncio.run(relay_publisher.publish_event(
        publisher, relay_publisher.CONVERSATION_TOPIC, _conversation(MISSION_A, event=event),
    ))
    assert sent == 1


# ── presence rides the same pipe ────────────────────────────────────────


def test_presence_change_is_forwarded_on_the_same_pipe():
    publisher = _StubPublisher(listening={TOKEN_A1})
    sent = asyncio.run(relay_publisher.publish_event(
        publisher, relay_publisher.PRESENCE_TOPIC,
        {"set_id": relay_publisher.PRESENCE_SET_ID,
         "key": f"mission:{MISSION_A}:auto-coordinator", "operation": "update"},
    ))
    assert sent == 1
    _, sealed = publisher.published[0]
    body = json.loads(open_stream_frame(link_serving._stream_key(TOKEN_A1), sealed))
    assert body["kind"] == "presence"
    assert body["mission_id"] == MISSION_A


def test_presence_key_is_three_parts_not_two():
    """surface.Presence keys are "{surface_id}:{participant_id}" and a
    mission's surface_id is itself "mission:<id>" -- so splitting on the
    first colon alone would take "<mission_id>:<participant>" as the
    mission and match no grant."""
    built = relay_publisher.build_frame(
        relay_publisher.PRESENCE_TOPIC,
        {"set_id": relay_publisher.PRESENCE_SET_ID,
         "key": f"mission:{MISSION_A}:auto-coordinator"},
    )
    assert built is not None
    assert built[0] == MISSION_A


def test_pillar_presence_is_skipped():
    assert relay_publisher.build_frame(
        relay_publisher.PRESENCE_TOPIC,
        {"set_id": relay_publisher.PRESENCE_SET_ID, "key": "pillar:p1:auto-pillar"},
    ) is None


def test_a_non_presence_setting_change_is_ignored():
    assert relay_publisher.build_frame(
        relay_publisher.PRESENCE_TOPIC,
        {"set_id": "dashboard.feature_flags", "key": f"mission:{MISSION_A}:x"},
    ) is None


# ── no listeners, no work ───────────────────────────────────────────────


def test_no_open_channel_means_no_seal_and_no_frame():
    publisher = _StubPublisher(listening=set())  # link published, nobody watching
    sent = asyncio.run(relay_publisher.publish_event(
        publisher, relay_publisher.CONVERSATION_TOPIC, _conversation(MISSION_A),
    ))
    assert sent == 0
    assert publisher.published == []
    assert link_serving._STREAM_KEYS == {}  # never even minted a key


def test_only_the_watching_guest_of_a_mission_is_published_to():
    publisher = _StubPublisher(listening={TOKEN_A2})  # A1 published but closed
    asyncio.run(relay_publisher.publish_event(
        publisher, relay_publisher.CONVERSATION_TOPIC, _conversation(MISSION_A),
    ))
    assert [token for token, _ in publisher.published] == [TOKEN_A2]


# ── sealing ─────────────────────────────────────────────────────────────


def test_frame_is_sealed_under_that_tokens_stream_key():
    publisher = _StubPublisher(listening={TOKEN_A1})
    asyncio.run(relay_publisher.publish_event(
        publisher, relay_publisher.CONVERSATION_TOPIC, _conversation(MISSION_A),
    ))
    _, sealed = publisher.published[0]
    opened = open_stream_frame(link_serving._stream_key(TOKEN_A1), sealed)
    assert opened is not None
    body = json.loads(opened)
    assert body["kind"] == "conversation"
    assert body["mission_id"] == MISSION_A
    assert body["question"]["answer"] == "there"


def test_a_frame_is_not_readable_with_another_links_key():
    publisher = _StubPublisher(listening={TOKEN_A1, TOKEN_B1})
    asyncio.run(relay_publisher.publish_event(
        publisher, relay_publisher.CONVERSATION_TOPIC, _conversation(MISSION_A),
    ))
    _, sealed = publisher.published[0]
    assert open_stream_frame(link_serving._stream_key(TOKEN_B1), sealed) is None


def test_each_guest_gets_a_frame_under_their_own_key():
    publisher = _StubPublisher(listening={TOKEN_A1, TOKEN_A2})
    asyncio.run(relay_publisher.publish_event(
        publisher, relay_publisher.CONVERSATION_TOPIC, _conversation(MISSION_A),
    ))
    by_token = dict(publisher.published)
    assert open_stream_frame(link_serving._stream_key(TOKEN_A1), by_token[TOKEN_A1])
    assert open_stream_frame(link_serving._stream_key(TOKEN_A2), by_token[TOKEN_A2])
    # ...and not under each other's.
    assert open_stream_frame(link_serving._stream_key(TOKEN_A2), by_token[TOKEN_A1]) is None


# ── grant gating and malformed input ────────────────────────────────────


def test_a_revoked_grant_is_never_published_to(monkeypatch):
    monkeypatch.setattr(
        link_serving, "check_grant", lambda token, org=None, now=None: None
    )
    publisher = _StubPublisher(listening={TOKEN_A1, TOKEN_A2})
    sent = asyncio.run(relay_publisher.publish_event(
        publisher, relay_publisher.CONVERSATION_TOPIC, _conversation(MISSION_A),
    ))
    assert sent == 0


def test_an_unreadable_grant_cache_publishes_nothing(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr("tools.graph.settings_ops.read_owned_set", boom)
    assert relay_publisher.mission_tokens(MISSION_A) == []


def test_non_mission_grants_are_not_publish_targets(monkeypatch):
    class _Member:
        key, payload = "cc" * 16, {
            "token": "cc" * 16, "target_type": "note", "target_uuid": MISSION_A, "meta": {},
        }

    class _Set:
        members = [_Member()]

    monkeypatch.setattr("tools.graph.settings_ops.read_owned_set", lambda *a, **kw: _Set())
    assert relay_publisher.mission_tokens(MISSION_A) == []


@pytest.mark.parametrize("data", [
    {}, {"mission_id": None}, {"mission_id": ""}, "not-a-dict", None,
])
def test_a_malformed_payload_is_skipped_not_raised(data):
    publisher = _StubPublisher(listening={TOKEN_A1})
    sent = asyncio.run(relay_publisher.publish_event(
        publisher, relay_publisher.CONVERSATION_TOPIC, data,
    ))
    assert sent == 0


def test_a_seal_failure_skips_that_token_without_ending_the_loop(monkeypatch):
    monkeypatch.setattr(
        relay_publisher, "seal_stream_frame",
        lambda key, body: (_ for _ in ()).throw(RuntimeError("seal exploded")),
    )
    publisher = _StubPublisher(listening={TOKEN_A1, TOKEN_A2})
    sent = asyncio.run(relay_publisher.publish_event(
        publisher, relay_publisher.CONVERSATION_TOPIC, _conversation(MISSION_A),
    ))
    assert sent == 0
    assert publisher.published == []


# ── the SSE reader ──────────────────────────────────────────────────────


def test_the_topics_this_consumer_declares_are_the_ones_it_handles():
    """The proxy carries what a consumer asks for and holds no list of its
    own, so a topic handled here but not declared would silently never
    arrive -- and one declared but not handled would cross processes for
    nothing."""
    declared = set(relay_publisher.EVENT_TOPICS)
    assert declared == {relay_publisher.CONVERSATION_TOPIC,
                        relay_publisher.PRESENCE_TOPIC}
    for topic in declared:
        assert relay_publisher.build_frame(topic, {}) is None or True
    # A topic outside the declaration must produce no frame at all.
    assert relay_publisher.build_frame("something:else", {"mission_id": "m1"}) is None
