"""Sharing an AUDITED credential into another organization's namespace:
unattended, re-sealed through the ordinary write path, value never on the wire."""
from types import SimpleNamespace

from starlette.applications import Starlette
from starlette.testclient import TestClient

from tools.dashboard import vault_routes
from tools.dashboard.api_auth import ApiPrincipal, ApiPrincipalKind
from tools.graph.schemas.vault_credential import (
    VAULT_AUDITED_SET_ID,
    VAULT_SECURED_SET_ID,
)

SRC = "blindhash"


def _member(key, value=None, vault_error=None):
    return SimpleNamespace(key=key, payload={"value": value} if value else {}, vault_error=vault_error)


def _client(monkeypatch, principal, *, members=(), existing=None, written=None):
    monkeypatch.setattr(vault_routes.api_auth, "require_authenticated_api_caller", lambda request: None)
    monkeypatch.setattr(vault_routes.api_auth, "principal_from_request", lambda request: principal)
    import tools.dashboard.vault_open_approvals as voa
    monkeypatch.setattr(voa, "_setting_route", lambda p, set_id, name: (f"{p.org}:{name}" if p.org else name, None))
    monkeypatch.setattr(vault_routes.settings_ops, "read_set", lambda set_id, *, org, peers=None: SimpleNamespace(members=list(members)))
    monkeypatch.setattr(vault_routes.settings_ops, "layers_for", lambda set_id, key, *, org: {"base": {"id": existing} if existing else None})

    def write_by_key(set_id, rev, key, payload, *, org, **kw):
        (written if written is not None else []).append((set_id, rev, key, payload, org))
        return "setting-new"

    monkeypatch.setattr(vault_routes.settings_ops, "write_by_key", write_by_key)
    return TestClient(Starlette(routes=vault_routes.ROUTES))


def _org_session(org=SRC):
    return ApiPrincipal(ApiPrincipalKind.ORG_SESSION, subject="sess-1", org=org)


def _url(name, set_id=VAULT_AUDITED_SET_ID):
    return f"/api/vault/credential/{set_id}/{name}/share"


def test_audited_share_reseals_into_the_destination_namespace(monkeypatch):
    written = []
    client = _client(monkeypatch, _org_session(),
                     members=[_member(f"{SRC}:dnsmadeeasy.api-key", "k-123")], written=written)
    r = client.post(_url("dnsmadeeasy.api-key"), json={"to_org": "autonomy"})
    assert r.status_code == 200, r.text
    assert r.json()["from_key"] == f"{SRC}:dnsmadeeasy.api-key"
    assert r.json()["to_key"] == "autonomy:dnsmadeeasy.api-key"
    # written through write_by_key with the BARE name and org=destination, so
    # the ordinary writeback + vaulted seal apply; value never in the response
    assert written == [(VAULT_AUDITED_SET_ID, 1, "dnsmadeeasy.api-key", {"value": "k-123"}, "autonomy")]
    assert "k-123" not in r.text


def test_secured_set_is_refused_with_the_ceremony_message(monkeypatch):
    client = _client(monkeypatch, _org_session())
    r = client.post(_url("x", VAULT_SECURED_SET_ID), json={"to_org": "autonomy"})
    assert r.status_code == 400 and "auto-2pgaq" in r.json()["error"]


def test_same_org_is_refused(monkeypatch):
    client = _client(monkeypatch, _org_session("autonomy"), members=[_member("autonomy:x", "v")])
    r = client.post(_url("x"), json={"to_org": "autonomy"})
    assert r.status_code == 400 and "already in" in r.json()["error"]


def test_cold_vault_is_a_503_not_a_wrong_value(monkeypatch):
    client = _client(monkeypatch, _org_session(),
                     members=[_member(f"{SRC}:x", None, vault_error=SimpleNamespace(reason="VAULT_NO_KEY_HOLDER"))])
    r = client.post(_url("x"), json={"to_org": "autonomy"})
    assert r.status_code == 503 and "cold" in r.json()["error"]


def test_existing_destination_needs_replace(monkeypatch):
    written = []
    client = _client(monkeypatch, _org_session(), members=[_member(f"{SRC}:x", "v")], existing="setting-old", written=written)
    r = client.post(_url("x"), json={"to_org": "autonomy"})
    assert r.status_code == 409 and written == []
    r = client.post(_url("x"), json={"to_org": "autonomy", "replace": True})
    assert r.status_code == 200 and len(written) == 1


def test_missing_source_is_404_scoped_to_the_caller(monkeypatch):
    client = _client(monkeypatch, _org_session(), members=[_member("someoneelse:x", "v")])
    r = client.post(_url("x"), json={"to_org": "autonomy"})
    assert r.status_code == 404


def test_bad_slug_is_refused(monkeypatch):
    client = _client(monkeypatch, _org_session(), members=[_member(f"{SRC}:x", "v")])
    r = client.post(_url("x"), json={"to_org": "Not A Slug!"})
    assert r.status_code == 400 and "to_org" in r.json()["error"]



# ---- deliver: unattended audited release into the caller's session ramfs ----

def _deliver_client(monkeypatch, principal, *, members=(), delivered=None, session_exists=True):
    client = _client(monkeypatch, principal, members=members)
    import tools.dashboard.dao.dashboard_db as ddb
    monkeypatch.setattr(ddb, "get_session", lambda name: {"tmux_name": name} if session_exists else None)
    import tools.dashboard.vault_release_delivery as vrd

    def deliver_payload(row, payload, *, now=None):
        (delivered if delivered is not None else []).append((row, payload))
        return {"delivery": "session-ramfs", "path": f"/run/secrets/{row['request']['setting']['key'].rsplit(':', 1)[-1]}"}

    monkeypatch.setattr(vrd, "deliver_payload", deliver_payload)
    return client


def _durl(name, set_id=VAULT_AUDITED_SET_ID):
    return f"/api/vault/credential/{set_id}/{name}/deliver"


def test_audited_deliver_writes_into_the_callers_session_ramfs(monkeypatch):
    delivered = []
    client = _deliver_client(monkeypatch, _org_session("autonomy"),
                             members=[_member("autonomy:openrouter.api-key", "sk-1")], delivered=delivered)
    r = client.post(_durl("openrouter.api-key"), json={"ttl_seconds": 0})
    assert r.status_code == 200, r.text
    assert r.json()["path"] == "/run/secrets/openrouter.api-key"
    assert "sk-1" not in r.text
    row, payload = delivered[0]
    assert row["session"] == "sess-1" and payload == {"value": "sk-1"}
    assert row["request"]["setting"] == {"set_id": VAULT_AUDITED_SET_ID, "key": "autonomy:openrouter.api-key"}


def test_deliver_refuses_secured_and_non_sessions_and_cold_vault(monkeypatch):
    client = _deliver_client(monkeypatch, _org_session("autonomy"), members=[_member("autonomy:x", "v")])
    assert client.post(_durl("x", VAULT_SECURED_SET_ID), json={}).status_code == 400
    client = _deliver_client(monkeypatch, _org_session("autonomy"), members=[_member("autonomy:x", "v")], session_exists=False)
    assert client.post(_durl("x"), json={}).status_code == 400
    client = _deliver_client(monkeypatch, _org_session("autonomy"),
                             members=[_member("autonomy:x", None, vault_error=SimpleNamespace(reason="cold"))])
    assert client.post(_durl("x"), json={}).status_code == 503
