"""org:join channel dispatch — the claim protocol over the viewer channel.

auto-4d6qm's connector half: an ``org:join`` grant's channel serves the
frozen claim_service surface (context/submit/status) instead of an
artifact. Tests run against a contract-shaped stub service, so they pin
the DISPATCH contract independently of auto-v3db2's implementation.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest

from tools.dashboard import link_serving

ORG = "joinorg"
TOKEN = "ab" * 32
CONTENT_TOKEN = "cd" * 32
INVITE_REF = "1a" * 32
OTHER_INVITE = "2b" * 32
TARGET_UUID = "11111111-1111-4111-8111-111111111111"
PERSONA = "3c" * 32


class StubService:
    """Records calls; returns canonical status-discriminated envelopes."""

    def __init__(self):
        self.calls = []

    def context(self, org, invite_ref):
        self.calls.append(("context", org, invite_ref))
        return {
            "status": "ok", "genesis_id": "9f" * 32, "heads": ["7e" * 32],
            "max_hlc": [1_800_000_000_000, 3], "granted_role": "member",
            "binding": "token", "invite_expiry": 1_800_000_100_000,
        }

    def submit(self, org, event_wire):
        self.calls.append(("submit", org, event_wire))
        return {"status": "pending", "have": 0, "need": 1}

    def status(self, org, invite_ref, persona_pub):
        self.calls.append(("status", org, invite_ref, persona_pub))
        return {"status": "pending", "have": 1, "need": 2, "approvals": []}


@pytest.fixture
def service(monkeypatch):
    """Patch the _claim_service() seam, NOT sys.modules: once the real
    module exists and has been imported, ``from tools.dashboard import
    claim_service`` resolves the package attribute and would bypass a
    sys.modules injection — making these tests pass alone but fail after
    any test that imports the real service."""
    stub = StubService()
    module = types.ModuleType("tools.dashboard.claim_service")
    module.context = stub.context
    module.submit = stub.submit
    module.status = stub.status
    monkeypatch.setattr(link_serving, "_claim_service", lambda: module)
    return stub


@pytest.fixture
def grants(monkeypatch):
    rows = {
        TOKEN: {
            "token": TOKEN, "target_type": "org:join",
            "target_uuid": TARGET_UUID, "invite_ref": INVITE_REF, "meta": {},
        },
        CONTENT_TOKEN: {
            "token": CONTENT_TOKEN, "target_type": "note",
            "target_uuid": TARGET_UUID, "meta": {},
        },
    }
    monkeypatch.setattr(
        link_serving, "check_grant", lambda token, org=None, now=None: rows.get(token)
    )
    return rows


def call(token, request):
    handler = link_serving.make_grant_handler(ORG)
    return asyncio.run(handler(token, json.dumps(request).encode()))


def envelope(raw: bytes) -> dict:
    return json.loads(raw.split(b"\n", 1)[0])


def _claim_wire(invite_ref=INVITE_REF) -> str:
    return json.dumps({"payload": {"type": "member.claim", "invite_ref": invite_ref}})


# -- the three ops dispatch verbatim ---------------------------------------------------


def test_context_op(service, grants):
    body = envelope(call(TOKEN, {"v": 1, "op": "context"}))
    assert body["status"] == "ok"
    assert body["granted_role"] == "member"
    assert body["max_hlc"] == [1_800_000_000_000, 3]  # A3 skew fix reaches the client
    assert service.calls == [("context", ORG, INVITE_REF)]  # grant supplies the locator


def test_submit_op(service, grants):
    wire = _claim_wire()
    body = envelope(call(TOKEN, {"v": 1, "op": "submit", "event": wire}))
    assert body == {"v": 1, "status": "pending", "have": 0, "need": 1}
    assert service.calls == [("submit", ORG, wire.encode("utf-8"))]


def test_status_op(service, grants):
    body = envelope(call(TOKEN, {"v": 1, "op": "status", "persona_pub": PERSONA}))
    assert body["status"] == "pending"
    assert service.calls == [("status", ORG, INVITE_REF, PERSONA)]


# -- scoping and cross-refusal ---------------------------------------------------------


def test_submit_refuses_a_foreign_invite_ref(service, grants):
    """Defence in depth: the channel is scoped to its grant's invitation."""
    raw = call(TOKEN, {"v": 1, "op": "submit", "event": _claim_wire(OTHER_INVITE)})
    assert raw == link_serving.REFUSED
    assert service.calls == []  # never reached the service


def test_content_token_cannot_serve_join_ops(service, grants):
    for op in ("context", "submit", "status"):
        request = {"v": 1, "op": op, "event": _claim_wire(), "persona_pub": PERSONA}
        assert call(CONTENT_TOKEN, request) == link_serving.REFUSED
    assert service.calls == []


def test_join_token_cannot_serve_content_ops(service, grants):
    for op in ("fetch", "head"):
        assert call(TOKEN, {"v": 1, "op": op}) == link_serving.REFUSED


def test_unknown_token_is_the_uniform_refusal(service, grants):
    assert call("ff" * 32, {"v": 1, "op": "context"}) == link_serving.REFUSED


# -- malformed input and service faults ------------------------------------------------


@pytest.mark.parametrize(
    "request_body",
    [
        {"v": 2, "op": "context"},
        {"v": 1, "op": "nope"},
        {"v": 1, "op": "submit"},               # missing event
        {"v": 1, "op": "submit", "event": ""},
        {"v": 1, "op": "submit", "event": "not json"},
        {"v": 1, "op": "status"},               # missing persona_pub
    ],
)
def test_malformed_requests(service, grants, request_body):
    raw = call(TOKEN, request_body)
    assert raw in (link_serving.BAD_REQUEST, link_serving.REFUSED)
    assert service.calls == []


def test_absent_service_serves_the_refusal(grants, monkeypatch):
    """This dispatch lands BEFORE v3db2: no service module → refusal, and
    a join channel never leaks that the token is valid."""
    monkeypatch.setitem(sys.modules, "tools.dashboard.claim_service", None)
    monkeypatch.setattr(
        link_serving, "_claim_service", lambda: (_ for _ in ()).throw(ImportError())
    )
    assert call(TOKEN, {"v": 1, "op": "context"}) == link_serving.REFUSED


def test_service_fault_serves_the_refusal(service, grants, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("service exploded")

    monkeypatch.setattr(link_serving._claim_service(), "context", boom)
    assert call(TOKEN, {"v": 1, "op": "context"}) == link_serving.REFUSED


def test_grant_without_invite_ref_refuses(service, monkeypatch):
    monkeypatch.setattr(
        link_serving, "check_grant",
        lambda token, org=None, now=None: {
            "token": TOKEN, "target_type": "org:join",
            "target_uuid": TARGET_UUID, "meta": {},
        },
    )
    assert call(TOKEN, {"v": 1, "op": "context"}) == link_serving.REFUSED
    assert service.calls == []
