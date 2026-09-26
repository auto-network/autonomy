"""Boundary checks for the isolated OIDC gate runtime generator."""

import json
import os
import stat
import tomllib

import pytest

from tools.network.service_auth import GateConfig, create_runtime, render_caddyfile


def config(**changes):
    args = dict(
        name="proof-a", hosts=("proof-a.autonomy.example.net",),
        issuer="https://integrator-example.okta.com", client_id="example-client",
        port=8101,
    )
    return GateConfig(**(args | changes))


@pytest.mark.parametrize("changes", [
    {"hosts": ()}, {"hosts": ("*.example.com",)},
    {"hosts": ("example.com\nrespond hacked",)},
    {"hosts": ("example.com:443",)}, {"hosts": ("EXAMPLE.com",)},
    {"issuer": "http://example.com"}, {"issuer": "https://u:p@example.com"},
    {"issuer": "https://example.com?issuer=evil"},
    {"issuer": "https://example.com/#fragment"},
    {"client_id": "id\nprovider=evil"}, {"name": "../escape"},
    {"port": 80}, {"port": True}, {"port": 65536},
])
def test_reject_config_injection_and_ambiguous_origins(changes):
    with pytest.raises(ValueError):
        config(**changes)


def test_private_independent_secrets_and_confined_auth(tmp_path):
    secret = tmp_path / "client-secret"
    secret.write_text("test-only-client-secret")
    secret.chmod(0o600)
    first, second = tmp_path / "first", tmp_path / "second"
    create_runtime(config(), first, secret)
    create_runtime(config(name="proof-b", port=8102), second, secret)
    assert stat.S_IMODE(first.stat().st_mode) == 0o700
    for path in first.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert len((first / "cookie-secret").read_bytes()) == 32
    assert (first / "cookie-secret").read_bytes() != (second / "cookie-secret").read_bytes()
    settings = tomllib.loads((first / "oauth2-proxy.cfg").read_text())
    assert settings["code_challenge_method"] == "S256"
    assert settings["auth_request_response_mode"] == "form_post"
    assert settings["cookie_csrf_samesite"] == "none"
    assert settings["cookie_samesite"] == "lax"
    assert settings["cookie_expire"] == "15m"
    assert settings["session_cookie_minimal"] is True
    assert "cookie_domains" not in settings
    assert "redirect_url" not in settings
    assert settings["http_address"] == "127.0.0.1:4180"
    assert settings["trusted_proxy_ips"] == ["127.0.0.1/32", "::1/128"]
    assert settings["pass_access_token"] is False
    assert settings["pass_authorization_header"] is False
    assert settings["request_logging"] is False
    compose = json.loads((first / "compose.json").read_text())
    auth = compose["services"]["auth"]
    assert auth["network_mode"] == "service:gateway"
    assert "ports" not in auth and "networks" not in auth
    assert not {"mem_limit", "cpus", "pids_limit"} & auth.keys()
    assert auth["read_only"] is True
    assert auth["user"] == f"{os.getuid()}:{os.getgid()}"
    assert all("test-only-client-secret" not in (first / f).read_text()
               for f in ("compose.json", "oauth2-proxy.cfg", "Caddyfile"))


def test_never_overwrite_runtime_or_accept_public_secret(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("test-only-secret")
    secret.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        create_runtime(config(), tmp_path / "runtime", secret)
    secret.chmod(0o600)
    create_runtime(config(), tmp_path / "runtime", secret)
    with pytest.raises(FileExistsError):
        create_runtime(config(), tmp_path / "runtime", secret)


def test_reject_shared_or_artifact_runtime_before_reading_secret(tmp_path):
    for path in ("/workspace/output/oidc", "/workspace/repo/oidc", "/opt/autonomy-codex/oidc"):
        with pytest.raises(ValueError, match="local /tmp"):
            create_runtime(config(), path, tmp_path / "missing")


def test_caddy_has_exact_host_gate_and_trusted_callback_metadata():
    rendered = render_caddyfile(config(hosts=("a.example.com", "b.example.com")))
    assert "@host0 host a.example.com" in rendered
    assert "@host1 host b.example.com" in rendered
    assert rendered.count('header_up X-Forwarded-Proto "https"') == 4
    assert 'header_up X-Forwarded-Host "a.example.com"' in rendered
    assert "request_header -Authorization" in rendered
    assert "request_header -X-Auth-Request-*" in rendered
    assert 'respond "Unknown host" 421' in rendered
    assert "admin off" in rendered
    assert "log {" not in rendered
    # Explicit wildcard matcher: without it Caddy treats the destination as
    # a path matcher, and an unauthenticated request can fall through.
    assert "redir * /oauth2/start?rd=https%3A%2F%2Fa.example.com%2F 302" in rendered
