"""Organization configuration uses existing Settings and vault storage."""
import pytest

from tools.dashboard import service_auth
from tools.graph.schemas.service_auth import SERVICE_AUTH_SET_ID, SERVICE_AUTH_SECRET_SET_ID


@pytest.fixture
def store(monkeypatch):
    rows = {}
    monkeypatch.setattr(service_auth.settings_ops, "read_set_key", lambda sid, key, **kw: rows.get((kw.get("org"), sid, key)))
    def write(sid, revision, key, payload, **kw):
        rows[(kw.get("org"), sid, key)] = {"payload": payload}
    def upsert(sid, revision, key, payload, **kw):
        assert sid == SERVICE_AUTH_SET_ID, "Vault values must use write_by_key"
        write(sid, revision, key, payload, **kw)
    monkeypatch.setattr(service_auth.settings_ops, "upsert_by_key", upsert)
    monkeypatch.setattr(service_auth.settings_ops, "write_by_key", write)
    monkeypatch.setattr(service_auth.service_publication, "list_zones", lambda org: [{"zone": "anchore.serve.auto.network", "state": "active"}])
    return rows


def test_save_keeps_client_secret_out_of_public_projection(store):
    config = {"provider": "okta", "issuer": "https://example.okta.com", "client_id": "client", "default_access": "oidc", "client_secret": "private"}
    projected = service_auth.save_config("anchore", config)
    assert projected["configured"] is True
    assert projected["default_access"] == "oidc"
    assert "private" not in str(projected)
    assert store[("anchore", SERVICE_AUTH_SECRET_SET_ID, "default")]["payload"] == {"client_secret": "private"}
    assert "client_secret" not in store[("anchore", SERVICE_AUTH_SET_ID, "default")]["payload"]
    assert service_auth.configuration("another-org")["configured"] is False


def test_edit_preserves_secret_when_blank(store):
    config = {"provider": "okta", "issuer": "https://example.okta.com", "client_id": "client", "default_access": "oidc", "client_secret": "private"}
    service_auth.save_config("anchore", config)
    service_auth.save_config("anchore", {**config, "client_secret": "", "default_access": "public"})
    assert service_auth.configuration("anchore")["default_access"] == "public"
    assert store[("anchore", SERVICE_AUTH_SECRET_SET_ID, "default")]["payload"]["client_secret"] == "private"


def test_no_domain_is_setup_prerequisite(store, monkeypatch):
    monkeypatch.setattr(service_auth.service_publication, "list_zones", lambda org: [])
    with pytest.raises(service_auth.service_publication.ServicePublicationError, match="domain_required"):
        service_auth.save_config("anchore", {})


def test_edit_replaces_vaulted_secret(store):
    config = {"provider": "okta", "issuer": "https://example.okta.com", "client_id": "client", "default_access": "oidc", "client_secret": "first"}
    service_auth.save_config("anchore", config)
    service_auth.save_config("anchore", {**config, "client_secret": "replacement"})
    assert store[("anchore", SERVICE_AUTH_SECRET_SET_ID, "default")]["payload"] == {"client_secret": "replacement"}


def test_cookie_key_is_local_and_stable(store):
    first = service_auth.cookie_secret("anchore")
    assert len(first) == 32
    assert service_auth.cookie_secret("anchore") == first
    assert service_auth.cookie_secret("another") != first
