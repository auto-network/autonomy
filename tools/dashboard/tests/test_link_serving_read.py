"""The generic ``read`` op dispatch (auto-t2lz1).

The read mirror of test_link_serving_write.py, and deliberately the same
tests: link_serving resolves the grant, checks the target_type against a
default-off allowlist, resolves the identity this channel reads AS, and
forwards an opaque body to the module that owns reads for that
target_type. No mission vocabulary belongs in this file -- see
mission_control's own tests for what a mission read MEANS.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from tools.dashboard import link_serving

ORG = "readorg"
TOKEN = "ab" * 32
TARGET_UUID = "11111111-1111-4111-8111-111111111111"


def call(token, request):
    handler = link_serving.make_grant_handler(ORG)
    return asyncio.run(handler(token, json.dumps(request).encode()))


def envelope(raw: bytes) -> dict:
    return json.loads(raw.split(b"\n", 1)[0])


@pytest.fixture
def grants(monkeypatch):
    rows = {
        TOKEN: {
            "token": TOKEN, "target_type": "mission",
            "target_uuid": TARGET_UUID, "meta": {"participant_id": "guest:abc"},
        },
    }
    monkeypatch.setattr(
        link_serving, "check_grant", lambda token, org=None, now=None: rows.get(token)
    )
    return rows


@pytest.fixture
def stub_handler(monkeypatch):
    calls = []

    async def handler(identity, target_uuid, body):
        calls.append((identity, target_uuid, body))
        if body.get("boom"):
            raise RuntimeError("handler exploded")
        if body.get("reject"):
            return None
        return {"echo": body}

    monkeypatch.setattr(
        link_serving, "_read_dispatch",
        lambda target_type: handler if target_type == "mission" else None,
    )
    return calls


def test_read_dispatches_identity_and_body_opaquely(stub_handler, grants):
    body = envelope(call(TOKEN, {"v": 1, "op": "read", "body": {"kind": "pillars"}}))
    assert body == {"v": 1, "status": "ok", "echo": {"kind": "pillars"}}
    assert stub_handler == [("guest:abc", TARGET_UUID, {"kind": "pillars"})]


def test_read_refuses_target_type_without_a_registered_handler(monkeypatch, grants):
    monkeypatch.setattr(link_serving, "_read_dispatch", lambda target_type: None)
    assert call(TOKEN, {"v": 1, "op": "read", "body": {}}) == link_serving.REFUSED


def test_read_without_bound_identity_reaches_handler_as_empty(stub_handler, monkeypatch):
    # Reads are authorised by holding the link, NOT by a bound participant
    # (see _serve_read: requiring meta.participant_id here made every
    # anonymous link fail at everything after its first screen). An unbound
    # channel reaches the handler as the empty identity, which the handler
    # is free to refuse if it genuinely needs to know who is asking.
    monkeypatch.setattr(
        link_serving, "check_grant",
        lambda token, org=None, now=None: {
            "token": TOKEN, "target_type": "mission",
            "target_uuid": TARGET_UUID, "meta": {},
        },
    )
    body = envelope(call(TOKEN, {"v": 1, "op": "read", "body": {"kind": "pillars"}}))
    assert body == {"v": 1, "status": "ok", "echo": {"kind": "pillars"}}
    assert stub_handler == [("", TARGET_UUID, {"kind": "pillars"})]


def test_read_refuses_non_dict_or_missing_body(stub_handler, grants):
    assert call(TOKEN, {"v": 1, "op": "read", "body": "nope"}) == link_serving.BAD_REQUEST
    assert call(TOKEN, {"v": 1, "op": "read"}) == link_serving.BAD_REQUEST
    assert stub_handler == []


def test_read_handler_exception_serves_the_refusal(stub_handler, grants):
    assert call(TOKEN, {"v": 1, "op": "read", "body": {"boom": True}}) == link_serving.REFUSED


def test_read_handler_rejection_is_a_bad_request(stub_handler, grants):
    assert call(TOKEN, {"v": 1, "op": "read", "body": {"reject": True}}) == link_serving.BAD_REQUEST


def test_unknown_token_is_the_uniform_refusal(stub_handler):
    assert call("ff" * 32, {"v": 1, "op": "read", "body": {}}) == link_serving.REFUSED
    assert stub_handler == []


def test_read_enabled_target_types_is_a_real_allowlist():
    """Default-off: a target_type must be listed to serve reads at all."""
    assert link_serving.READ_ENABLED_TARGET_TYPES == frozenset({"mission"})
    for target_type in ("design", "present", "note", "org:join", "file", "nonsense"):
        assert link_serving._read_dispatch(target_type) is None
    assert link_serving._read_dispatch("mission") is not None


def test_a_float_bearing_payload_serializes(stub_handler, grants, monkeypatch):
    """REGRESSION (found only by the end-to-end acceptance test): these
    envelopes must NOT go through canonical_json, which forbids floats by
    design. Every Mission Control payload carries epoch float timestamps
    (created_at, answered_at), so a canonical encoder turns a guest's own
    question into a uniform refusal. The original stub payloads here were
    int-only, which is exactly why the unit tests missed it."""
    async def handler(identity, target_uuid, body):
        return {"questions": [{"entry_id": "e1", "created_at": 1_800_000_000.123}]}

    monkeypatch.setattr(link_serving, "_read_dispatch", lambda t: handler)
    body = envelope(call(TOKEN, {"v": 1, "op": "read", "body": {"kind": "questions"}}))
    assert body["status"] == "ok"
    assert body["questions"][0]["created_at"] == 1_800_000_000.123
