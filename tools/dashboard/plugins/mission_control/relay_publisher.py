"""Mission Control's live-update publisher (auto-8npih).

The READ half of Mission Control over the relay: an answer, a progress
update, a reopen, or a presence change reaches an open guest channel
without polling or a reload. The WRITE half (a guest asking or reopening
over the relay) is `link_serving._serve_write` and already ships.

WHY THIS IS AN SSE CLIENT AND NOT AN ``event_bus`` IMPORT. The connector
is a SEPARATE PROCESS from the dashboard (`link_serving.main()`, one per
org tunnel), so the dashboard's in-process bus object is empty here.
Mission Control's events arrive over the dashboard's own HTTP event
stream instead — ``GET /api/events``, the same endpoint every browser tab
already consumes. The subscription is UNFILTERED and that is the settled
decision (graph://248d2e36-4cc): one connection carrying every topic plus
a comparison on the incoming identifier, rather than one long-lived
connection per published link.

WHAT ONE PUBLISH COSTS. One seal and one frame per link token that
currently has a listener. Nothing is sealed for a link nobody has open,
so an org holding many quiet published links does no work per event.

FAN-OUT, HONESTLY. A mission link is personalized per guest today
(``meta.participant_id``, auto-xwamk), so each guest holds their own
token and each token's stream has exactly one listener — the audience
scaling the relay fan-out exists for is worth nothing to Mission Control
right now. It is still the correct mechanism: it is what makes
server-initiated push possible at all, which is the actual blocker, and
the scaling arrives with anonymous shared links (Milestone 2). Building a
second, per-guest push path instead is exactly what the shared-foundation
decision exists to prevent.
"""

from __future__ import annotations

import asyncio
import json
import logging

from tools.network.relaykit.channel import seal_stream_frame

logger = logging.getLogger(__name__)

#: Topics this publisher forwards. Everything else on the unfiltered
#: subscription is ignored by the comparison below.
CONVERSATION_TOPIC = "mission_control:conversation"
PRESENCE_TOPIC = "setting.changed"

#: Presence rides this same pipe rather than a second delivery mechanism
#: -- a presence change is just another event type on one subscription.
#: It reaches the bus as a Settings write, so it is recognised by the
#: set_id its surface rows live under.
PRESENCE_SET_ID = "dashboard.surface.presence"

#: What the generic event proxy must carry for this consumer. It asks; the
#: pipe holds no list of its own, which is what keeps it ignorant of us.
EVENT_TOPICS = (CONVERSATION_TOPIC, PRESENCE_TOPIC)

#: Wire version of the fan-out frame body, sealed under the link's stream
#: key. The guest widget's transport layer parses this after opening the
#: frame; ``kind`` tells it which of its two existing update paths to
#: take, matching the shapes it already handles over direct HTTP.
FRAME_VERSION = 1


def wants_event(topic: str, data) -> bool:
    """Dashboard-side pre-filter for the generic event proxy: the presence
    topic is the machine-wide ``setting.changed`` stream, of which only the
    presence set is ours. Everything else is decided by :func:`build_frame`
    on the connector, exactly as before."""
    if topic == PRESENCE_TOPIC:
        return isinstance(data, dict) and data.get("set_id") == PRESENCE_SET_ID
    return True


def build_frame(topic: str, data: dict) -> tuple[str, bytes] | None:
    """One bus event -> ``(mission_id, frame_json)``, or None to ignore.

    Returns the mission the event belongs to plus the bytes to seal. A
    payload that is malformed or belongs to no mission is skipped, never
    raised -- one bad event must not end the publish loop.
    """
    if not isinstance(data, dict):
        return None
    if topic == CONVERSATION_TOPIC:
        mission_id = data.get("mission_id")
        if not isinstance(mission_id, str) or not mission_id:
            return None
        return mission_id, json.dumps({
            "v": FRAME_VERSION,
            "kind": "conversation",
            "event": data.get("event"),
            "mission_id": mission_id,
            "pillar_id": data.get("pillar_id"),
            "entry_id": data.get("entry_id"),
            "question": data.get("question"),
            "update": data.get("update"),
        }).encode("utf-8")
    if topic == PRESENCE_TOPIC:
        if data.get("set_id") != PRESENCE_SET_ID:
            return None
        # The presence Settings key is "{surface_id}:{participant_id}"
        # (surface.Presence._key), and a mission's surface_id is itself
        # "mission:<mission_id>" -- so the whole key is THREE parts:
        # "mission:<mission_id>:<participant_id>". Splitting only on the
        # first colon would take "<mission_id>:<participant_id>" as the
        # mission and match no grant. A pillar's surface is
        # "pillar:<pillar_id>", which names no mission directly and is
        # skipped here; a pillar presence change reaches guests as part
        # of the conversation stream instead.
        key = data.get("key")
        if not isinstance(key, str):
            return None
        parts = key.split(":")
        if len(parts) < 3 or parts[0] != "mission" or not parts[1]:
            return None
        mission_id = parts[1]
        return mission_id, json.dumps({
            "v": FRAME_VERSION,
            "kind": "presence",
            "mission_id": mission_id,
            "surface": key,
        }).encode("utf-8")
    return None


def mission_tokens(mission_id: str, *, org: str | None = None) -> list[str]:
    """Every live link token pointing at *mission_id*.

    One mission may have MANY tokens: mission grants are personalized per
    guest, so each guest holds their own. Reads the same owning-scope
    grant cache ``check_grant`` gates on, and re-validates each row
    through it so a revoked or expired grant is never published to.
    """
    from tools.dashboard import link_serving
    from tools.graph import settings_ops

    try:
        members = settings_ops.read_owned_set(
            link_serving.NETWORK_LINK_GRANT_SET_ID,
            org=org,
            target_revision=link_serving.NETWORK_LINK_GRANT_REVISION,
        ).members
    except Exception:
        return []  # unreadable cache -> publish nothing (fail closed)
    tokens = []
    for member in members:
        payload = member.payload
        if not isinstance(payload, dict):
            continue
        if payload.get("target_type") != "mission":
            continue
        if payload.get("target_uuid") != mission_id:
            continue
        # Re-check through the real gate: TTL, require_auth, malformation.
        if link_serving.check_grant(member.key, org=org) is None:
            continue
        tokens.append(member.key)
    return tokens


async def publish_event(publisher, topic: str, data: dict, *, org: str | None = None) -> int:
    """Seal and fan out one bus event. Returns the number of frames sent.

    Zero is the ordinary case, not an error: nobody has that mission
    open, or the event is not one this publisher forwards.
    """
    built = build_frame(topic, data)
    if built is None:
        return 0
    mission_id, body = built
    from tools.dashboard import link_serving

    sent = 0
    for token in mission_tokens(mission_id, org=org):
        # Do no work for a link nobody is watching -- checked BEFORE the
        # seal, which is the expensive half.
        if not publisher.has_listeners(token):
            continue
        try:
            sealed = seal_stream_frame(link_serving._stream_key(token), body)
        except Exception:
            logger.warning("stream seal failed for mission %s", mission_id, exc_info=True)
            continue
        if await publisher.publish(token, sealed):
            sent += 1
    return sent
