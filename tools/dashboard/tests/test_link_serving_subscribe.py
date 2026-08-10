"""The subscribe op and its stream key (auto-albp6.8).

One 32-byte key per link, shared by every viewer of that link -- which is
exactly what lets ONE sealed frame be fanned out by the relay
(auto-albp6.7) instead of re-sealed per viewer. It is delivered over each
viewer's OWN pairwise channel, after check_grant passes, and never
reaches the relay.

The per-channel key still protects everything else on that channel; the
stream key is used only for fanned-out frames. See the two-key diagram in
graph://ce07a01f-faa.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from tools.dashboard import link_serving

ORG = "streamorg"
TOKEN = "ab" * 32
OTHER_TOKEN = "cd" * 32
NOTE_TOKEN = "ef" * 32
TARGET_UUID = "11111111-1111-4111-8111-111111111111"


def call(token, request):
    handler = link_serving.make_grant_handler(ORG)
    return asyncio.run(handler(token, json.dumps(request).encode()))


def envelope(raw: bytes) -> dict:
    return json.loads(raw.split(b"\n", 1)[0])


@pytest.fixture(autouse=True)
def _clean_keys():
    link_serving._STREAM_KEYS.clear()
    yield
    link_serving._STREAM_KEYS.clear()


@pytest.fixture
def grants(monkeypatch):
    rows = {
        TOKEN: {
            "token": TOKEN, "target_type": "mission",
            "target_uuid": TARGET_UUID, "meta": {"participant_id": "guest:a"},
        },
        OTHER_TOKEN: {
            "token": OTHER_TOKEN, "target_type": "mission",
            "target_uuid": TARGET_UUID, "meta": {"participant_id": "guest:b"},
        },
        NOTE_TOKEN: {
            "token": NOTE_TOKEN, "target_type": "note",
            "target_uuid": TARGET_UUID, "meta": {},
        },
    }
    monkeypatch.setattr(
        link_serving, "check_grant", lambda token, org=None, now=None: rows.get(token)
    )
    return rows


def test_subscribe_on_a_stream_grant_returns_the_stream_key(grants):
    body = envelope(call(TOKEN, {"v": 1, "op": "subscribe"}))
    assert body["status"] == "ok"
    assert len(bytes.fromhex(body["stream_key"])) == 32


def test_key_is_stable_across_subscribers_of_one_link(grants):
    """Every viewer of a link gets the SAME key -- the property the whole
    fan-out depends on."""
    first = envelope(call(TOKEN, {"v": 1, "op": "subscribe"}))["stream_key"]
    second = envelope(call(TOKEN, {"v": 1, "op": "subscribe"}))["stream_key"]
    assert first == second


def test_key_differs_between_links(grants):
    a = envelope(call(TOKEN, {"v": 1, "op": "subscribe"}))["stream_key"]
    b = envelope(call(OTHER_TOKEN, {"v": 1, "op": "subscribe"}))["stream_key"]
    assert a != b


def test_subscribe_on_a_note_grant_is_the_uniform_refusal(grants):
    assert call(NOTE_TOKEN, {"v": 1, "op": "subscribe"}) == link_serving.REFUSED
    assert NOTE_TOKEN not in link_serving._STREAM_KEYS  # no key ever minted


def test_subscribe_with_an_unknown_token_yields_no_key(grants):
    assert call("ff" * 32, {"v": 1, "op": "subscribe"}) == link_serving.REFUSED
    assert link_serving._STREAM_KEYS == {}


def test_every_refusal_is_byte_identical(grants):
    """A revoked/unknown token and a wrong-target-type token must not be
    distinguishable -- anti-enumeration."""
    unknown = call("ff" * 32, {"v": 1, "op": "subscribe"})
    wrong_type = call(NOTE_TOKEN, {"v": 1, "op": "subscribe"})
    assert unknown == wrong_type == link_serving.REFUSED


def test_revoked_grant_yields_no_key(monkeypatch):
    """check_grant runs BEFORE the key is handed over, which is what
    makes revocation work with no key machinery at all."""
    monkeypatch.setattr(
        link_serving, "check_grant", lambda token, org=None, now=None: None
    )
    assert call(TOKEN, {"v": 1, "op": "subscribe"}) == link_serving.REFUSED
    assert link_serving._STREAM_KEYS == {}


def test_forget_stream_key_drops_it(grants):
    original = envelope(call(TOKEN, {"v": 1, "op": "subscribe"}))["stream_key"]
    link_serving.forget_stream_key(TOKEN)
    assert TOKEN not in link_serving._STREAM_KEYS
    fresh = envelope(call(TOKEN, {"v": 1, "op": "subscribe"}))["stream_key"]
    assert fresh != original  # a republished token cannot reuse an old key


def test_stream_enabled_target_types_is_a_real_allowlist():
    assert link_serving.STREAM_ENABLED_TARGET_TYPES == frozenset({"mission"})


def test_content_ops_still_work_on_a_stream_grant(grants, monkeypatch):
    """The subscribe op is additive: fetch/head behaviour on the same
    grant is untouched."""
    monkeypatch.setattr(
        link_serving, "resolve_target",
        lambda grant, org=None: {"kind": "mission", "viewer": b"<html></html>"},
    )
    monkeypatch.setattr(link_serving, "_resolve_org_brand", lambda org: None)
    body = envelope(call(TOKEN, {"v": 1, "op": "head"}))
    assert body["status"] == "ok"
