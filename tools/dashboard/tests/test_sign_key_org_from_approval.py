"""The sign-key route resolves the organization from the request being signed.

The operator's browser carries no organization -- it owns them all, and the
cookie that proves that says nothing about which. So resolving "the caller's
org" returned nothing and the route took a 404 branch before looking anything
up. Four signing attempts failed that way, each surfaced to the operator as
"no signing key is configured", which named the wrong problem and made the
next attempt look identical to the last.

The organization comes from the approval row instead: server-owned, already
shown to the operator, and not a selector the browser can aim elsewhere.
"""
from __future__ import annotations

import asyncio

import pytest

from tools.dashboard import approvals_routes as ar
from tools.graph import settings_ops


class _Req:
    def __init__(self, qp=None):
        self.query_params = qp or {}
        self.headers = {}
        self.method = "GET"


@pytest.fixture(autouse=True)
def _authorized(monkeypatch):
    monkeypatch.setattr(ar.api_auth, "require_global_api_authority",
                        lambda request: None)


def test_the_org_comes_from_the_approval(monkeypatch):
    monkeypatch.setattr(ar.ar, "get", lambda rid: {"id": rid, "session": "auto-x"})
    monkeypatch.setattr(ar, "_org_for_approval", lambda rid: "anchore")
    seen = {}

    def fake_read(set_id, key, *, org, peers):
        seen["key"], seen["org"] = key, org
        return {"payload": {"armored_private_key": "-----BEGIN PGP PRIVATE KEY BLOCK-----x"}}

    monkeypatch.setattr(settings_ops, "read_set_key", fake_read)

    resp = asyncio.run(ar.get_sign_key(_Req({"approval": "abc"})))

    assert resp.status_code == 200
    assert seen == {"key": "anchore", "org": "personal"}, (
        "the key is looked up by the approval's org, in the operator's store")


def test_a_request_that_names_no_organization_says_so(monkeypatch):
    """Not 404. "No key configured" sent the operator looking for a missing
    row when the request simply had not said which key it wanted."""
    monkeypatch.setattr(ar, "_org_for_approval", lambda rid: None)
    monkeypatch.setattr(settings_ops, "_resolve_settings_caller",
                        lambda org: None)

    resp = asyncio.run(ar.get_sign_key(_Req()))

    assert resp.status_code == 400
    assert b"which organization" in resp.body


def test_a_missing_key_names_the_org_and_where_to_put_it(monkeypatch):
    monkeypatch.setattr(ar, "_org_for_approval", lambda rid: "blindhash")
    monkeypatch.setattr(settings_ops, "read_set_key",
                        lambda *a, **k: None)

    resp = asyncio.run(ar.get_sign_key(_Req({"approval": "abc"})))

    assert resp.status_code == 404
    assert b"blindhash" in resp.body, "the org has to be named to be actionable"


def test_the_browser_cannot_select_an_organization(monkeypatch):
    """?org= is deliberately not honoured. require_global_api_authority tells
    handlers not to re-parse caller-controlled organization selectors, and a
    selector for somebody's private key is the last place to start."""
    monkeypatch.setattr(ar, "_org_for_approval", lambda rid: None)
    monkeypatch.setattr(settings_ops, "_resolve_settings_caller",
                        lambda org: None)

    resp = asyncio.run(ar.get_sign_key(_Req({"org": "anchore"})))

    assert resp.status_code == 400
