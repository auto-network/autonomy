"""auto-k784w backend: the launch chooser's account list (no secret part,
operator only, recommended = what the picker would choose) and the strict
`account` launch field (graph://7eb29bc8-31a v6 §11, Delta 1 and 3)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from agents import session_launcher as sl
from tools.dashboard import api_auth, harness_accounts, unlock_routes
from tools.dashboard.tests.test_remote_api_authority import OPERATOR, Principal, _org
from tools.graph import harness_credentials as hv

SECRETS = ("at-SECRET", "rt-SECRET", "setup-SECRET")


def _vault(monkeypatch, harness="claude", accounts=None):
    accounts = accounts if accounts is not None else [
        hv.Account("claude", "A", {"alias": "max", "email": "a@x", "access": "at-SECRET",
                                   "refresh": "rt-SECRET", "setup": "setup-SECRET"}),
        hv.Account("claude", "B", {"alias": "pro", "email": "b@x", "access": "at-SECRET",
                                   "refresh": "rt-SECRET"}),
        hv.Account("claude", "C", {"alias": "broken"}),
    ]
    monkeypatch.setattr(hv, "list_accounts", lambda h, **_k: [a for a in accounts if a.harness == h])
    monkeypatch.setattr(hv, "list_public", lambda h, **_k: [a for a in accounts if a.harness == h])
    monkeypatch.setattr(hv, "all_public", lambda h, **_k: [a for a in accounts if a.harness == h])
    monkeypatch.setattr(hv, "read_credential", lambda h, i, **_k: next(
        (a for a in accounts if a.harness == h and a.id == i), None))
    monkeypatch.setattr(hv, "read_account", lambda h, i, **_k: next(
        (a for a in accounts if a.harness == h and a.id == i), None))


def _readings(monkeypatch, rows):
    monkeypatch.setattr(harness_accounts, "_readings", lambda h: rows)
    monkeypatch.setattr(sl, "_claude_usage_rows", lambda: list(rows.values()))
    monkeypatch.setattr(sl, "_reading_window_open", lambda p, now: True)
    monkeypatch.setattr(sl, "_usage_exhausted",
                        lambda p, now: bool(p) and p["windows"]["short"]["used_percent"] >= 100)


def _reading(account, short, plan="max"):
    return {"harness": "claude", "account_id": account, "updated_at": "2026-10-02T05:00:00Z",
            "status": "ok", "plan_type": plan, "source": "probe",
            "windows": {"short": {"used_percent": short, "resets_at": 1},
                        "long": {"used_percent": 10, "resets_at": 2}}}


def test_rows_carry_no_secret_and_recommend_what_the_picker_would(monkeypatch):
    _vault(monkeypatch)
    _readings(monkeypatch, {"A": _reading("A", 80), "B": _reading("B", 10, "pro")})
    rows = harness_accounts.account_rows("claude")
    assert all(s not in json.dumps(rows) for s in SECRETS)
    assert [r["account_id"] for r in rows] == ["B", "A", "C"]
    b = rows[0]
    assert (b["recommended"], b["launchable"], b["exhausted"], b["plan_type"]) == (True, True, False, "pro")
    assert b["usage"]["short"] == {"used_percent": 10, "resets_at": 1}
    assert rows[2]["launchable"] is False and rows[2]["usage"] is None


def test_an_exhausted_account_is_marked_and_not_recommended(monkeypatch):
    _vault(monkeypatch)
    _readings(monkeypatch, {"A": _reading("A", 100), "B": _reading("B", 40)})
    rows = {r["account_id"]: r for r in harness_accounts.account_rows("claude")}
    assert rows["A"]["exhausted"] and not rows["A"]["recommended"] and rows["B"]["recommended"]


def test_no_recommendation_when_the_picker_would_choose_at_random(monkeypatch):
    _vault(monkeypatch)
    _readings(monkeypatch, {"A": _reading("A", 10)})          # B has no reading
    assert not any(r["recommended"] for r in harness_accounts.account_rows("claude"))


def test_check_account_is_strict(monkeypatch):
    _vault(monkeypatch)
    assert harness_accounts.check_account("claude", "A") is None
    assert harness_accounts.check_account("claude", "Z")[0] == "account-not-found"
    assert harness_accounts.check_account("claude", "C")[0] == "account-not-launchable"


@pytest.mark.parametrize("principal, status", [(OPERATOR, 200), (_org("alpha"), 403), (None, 401)])
def test_the_route_is_the_operators_only(monkeypatch, principal, status):
    from tools.dashboard import server

    _vault(monkeypatch)
    _readings(monkeypatch, {})
    monkeypatch.setattr(unlock_routes, "gate_enforced", lambda: True)
    app = Starlette(routes=[Route("/api/harness/accounts", server.api_harness_accounts)],
                    middleware=[Middleware(Principal)])
    Principal.principal = principal
    try:
        with TestClient(app) as client:
            response = client.get("/api/harness/accounts?harness=claude")
    finally:
        Principal.principal = None
    assert response.status_code == status
    if status == 200:
        assert [r["account_id"] for r in response.json()["accounts"]][:1]


def test_a_launch_naming_an_unknown_account_is_refused_before_anything_registers(monkeypatch):
    from tools.dashboard import server

    _vault(monkeypatch)
    monkeypatch.setattr(server.workspace_settings, "get_workspace",
                        lambda p: SimpleNamespace(id=p, harness="claude"))

    async def register_pending(*_a, **_k):
        raise AssertionError("registered")

    monkeypatch.setattr(server.session_monitor, "register_pending", register_pending)
    response = asyncio.run(server._create_session_from_body(
        {"project": "dev", "account": "Z"}, None))
    assert response.status_code == 409 and json.loads(response.body)["refusal"] == "account-not-found"


# ── the launcher honours the choice strictly ───────────────────────────────


def test_the_claude_picker_takes_exactly_the_chosen_account(monkeypatch):
    _vault(monkeypatch)
    _readings(monkeypatch, {"A": _reading("A", 90), "B": _reading("B", 1)})
    creds = sl._resolve_credentials_via_substrate(prefer_alias=None, account_id="A")
    assert (creds["harness_token"], creds["selection"]["method"]) == ("A", "explicit")
    assert sl._resolve_credentials_via_substrate(prefer_alias=None, account_id="Z") is None
