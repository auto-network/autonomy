"""Deployment ownership and least-authority compatibility delivery."""
from pathlib import Path

import pytest
import yaml
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tools.dashboard import api_auth, voice_commit_routes


def test_compose_voice_has_independent_process_and_loopback_publication():
    root = Path(__file__).resolve().parents[3]
    compose = yaml.safe_load((root / "docker-compose.yml").read_text())
    voice = compose["services"]["voice-gateway"]
    assert voice["profiles"] == ["voice"]
    assert voice["ports"] == ["127.0.0.1:${VOICE_BACKEND_PORT:-8082}:8082"]
    assert voice["restart"] == "unless-stopped"
    assert voice["user"] == "1000:1000"
    assert "networks" not in voice, "reuse the pinned default network"
    assert not any("docker.sock" in str(volume) or "/tmp" in str(volume) for volume in voice["volumes"])
    script = (root / "deploy/serve-voice.sh").read_text()
    assert "--reload" not in script
    assert "tools.dashboard.voice_gateway:app" in script
    assert "--ssl-certfile" in script and "--ssl-keyfile" in script


@pytest.mark.parametrize("kind,scope,audience,status", [
    (api_auth.ApiPrincipalKind.COMPATIBILITY, None, None, 403),
    (api_auth.ApiPrincipalKind.OPERATOR_COOKIE, None, None, 403),
    (api_auth.ApiPrincipalKind.EXTERNAL_SERVICE, "other", "dashboard-local", 403),
    (api_auth.ApiPrincipalKind.EXTERNAL_SERVICE, "voice-sidecar", "other", 403),
    (api_auth.ApiPrincipalKind.EXTERNAL_SERVICE, "voice-sidecar", "dashboard-local", 200),
])
def test_commit_requires_exact_service_scope(monkeypatch, kind, scope, audience, status):
    from tools.dashboard import tmux_send
    delivered = []

    async def deliver(bind, text):
        delivered.append((bind, text))

    monkeypatch.setattr(tmux_send, "tmux_send_awaited", deliver)
    principal = api_auth.ApiPrincipal(
        kind=kind, application_scope=scope, resource_audience=audience,
        api_capabilities=(("POST", voice_commit_routes.PATH),),
    )
    monkeypatch.setattr(api_auth, "principal_from_request", lambda request: principal)
    with TestClient(Starlette(routes=voice_commit_routes.ROUTES)) as client:
        response = client.post(voice_commit_routes.PATH, json={"bind": "target", "text": "draft"})
    assert response.status_code == status
    assert delivered == ([("target", "draft")] if status == 200 else [])
