"""The generic ``write`` op dispatch — link_serving.py's own mechanics,
independent of any target_type's write semantics.

link_serving.py never interprets a write's body. It resolves the grant,
checks whether this grant's target_type is write-enabled
(``WRITE_ENABLED_TARGET_TYPES``), resolves the identity the channel is
bound to (from the grant's own ``meta.participant_id`` — never a
client-supplied value), and forwards ``(identity, target_uuid, body)`` to
whatever handler that target_type registered via ``_write_dispatch``.
These tests exercise exactly that mechanism with a stub handler — no
mission vocabulary anywhere here (see
``tools/dashboard/plugins/mission_control/tests/test_api.py`` for
``handle_relay_write``'s own mission-specific behavior).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from tools.dashboard import link_serving

ORG = "writeorg"
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
    """Records every call it's given; behavior is driven by the body so
    a test can force the exception / rejection paths without a second
    fixture."""
    calls = []

    async def handler(identity, target_uuid, body):
        calls.append((identity, target_uuid, body))
        if body.get("boom"):
            raise RuntimeError("handler exploded")
        if body.get("reject"):
            return None
        return {"echo": body}

    monkeypatch.setattr(
        link_serving, "_write_dispatch",
        lambda target_type: handler if target_type == "mission" else None,
    )
    return calls


def test_write_dispatches_identity_and_body_opaquely(stub_handler, grants):
    body = envelope(call(TOKEN, {"v": 1, "op": "write", "body": {"x": 1}}))
    assert body == {"v": 1, "status": "ok", "echo": {"x": 1}}
    assert stub_handler == [("guest:abc", TARGET_UUID, {"x": 1})]


def test_write_refuses_target_type_without_a_registered_handler(monkeypatch, grants):
    monkeypatch.setattr(link_serving, "_write_dispatch", lambda target_type: None)
    assert call(TOKEN, {"v": 1, "op": "write", "body": {}}) == link_serving.REFUSED


def test_write_refuses_missing_bound_identity(stub_handler, monkeypatch):
    monkeypatch.setattr(
        link_serving, "check_grant",
        lambda token, org=None, now=None: {
            "token": TOKEN, "target_type": "mission",
            "target_uuid": TARGET_UUID, "meta": {},
        },
    )
    assert call(TOKEN, {"v": 1, "op": "write", "body": {}}) == link_serving.REFUSED
    assert stub_handler == []  # never reached the handler


def test_write_refuses_non_dict_or_missing_body(stub_handler, grants):
    assert call(TOKEN, {"v": 1, "op": "write", "body": "nope"}) == link_serving.BAD_REQUEST
    assert call(TOKEN, {"v": 1, "op": "write"}) == link_serving.BAD_REQUEST
    assert stub_handler == []


def test_write_handler_exception_serves_the_refusal(stub_handler, grants):
    raw = call(TOKEN, {"v": 1, "op": "write", "body": {"boom": True}})
    assert raw == link_serving.REFUSED


def test_write_handler_rejection_is_a_bad_request(stub_handler, grants):
    raw = call(TOKEN, {"v": 1, "op": "write", "body": {"reject": True}})
    assert raw == link_serving.BAD_REQUEST


def test_unknown_token_is_the_uniform_refusal(stub_handler):
    assert call("ff" * 32, {"v": 1, "op": "write", "body": {}}) == link_serving.REFUSED
    assert stub_handler == []


def test_write_enabled_target_types_is_a_real_allowlist():
    """Disabled by default, not implicit in an if/else -- a target_type
    must be listed to accept writes at all."""
    assert link_serving.WRITE_ENABLED_TARGET_TYPES == frozenset({"mission"})
    for target_type in ("design", "present", "note", "org:join", "file", "nonsense"):
        assert link_serving._write_dispatch(target_type) is None
    assert link_serving._write_dispatch("mission") is not None
