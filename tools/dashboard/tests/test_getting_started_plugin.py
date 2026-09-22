"""Getting Started plugin: the harness sign-in scan API (record v12 FR7a)."""
from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tools.dashboard import api_auth, route_policy
from tools.dashboard.plugins.getting_started.entrypoints import api as gs_api
from tools.graph import credential_import as ci


@pytest.fixture
def client(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / ".grok").mkdir(parents=True)
    (home / ".grok" / "auth.json").write_text('{"access_token": "g"}')
    monkeypatch.setattr(ci, "operator_home", lambda: str(home))
    calls: list[bool] = []

    def fake_import(h, **kw):
        calls.append(kw.get("dry_run"))
        assert h == str(home)
        report = ci.ImportReport()
        report.add(ci.HarnessResult("claude", ci.STATUS_NEEDS_SIGN_IN, "no file"))
        report.add(ci.HarnessResult("codex", ci.STATUS_NEEDS_SIGN_IN, "no file"))
        report.add(ci.HarnessResult(
            "grok", ci.STATUS_WOULD_IMPORT if kw.get("dry_run") else ci.STATUS_IMPORTED, "sealed",
        ))
        return report
    monkeypatch.setattr(ci, "run_import", fake_import)

    def authenticate(request):
        if request.headers.get("authorization") == "Bearer op":
            return ("op-session", None), None
        return None, None
    app = Starlette(routes=route_policy.apply_default_deny(gs_api.routes, plugin=True))
    app.add_middleware(
        api_auth.ApiIdentityMiddleware, authenticate_bearer=authenticate,
        verify_cookie=lambda token: None, cookie_name="test-session",
    )
    with TestClient(app) as c:
        yield c, calls


def test_scan_requires_authentication(client):
    c, _ = client
    assert c.get("/api/plugins/getting_started/harnesses").status_code in (401, 403)


def test_scan_reports_three_harnesses_and_usable(client):
    c, calls = client
    r = c.get("/api/plugins/getting_started/harnesses", headers={"Authorization": "Bearer op"})
    assert r.status_code == 200
    body = r.json()
    assert [h["harness"] for h in body["harnesses"]] == ["claude", "codex", "grok"]
    assert body["usable"] == ["grok"]
    assert calls == [True]


def test_import_runs_the_scan_with_writes(client):
    c, calls = client
    r = c.post("/api/plugins/getting_started/harnesses/import", headers={"Authorization": "Bearer op"})
    assert r.status_code == 200
    assert calls == [False]
