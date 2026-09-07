"""Stable voice ingress and per-login buffer ownership contracts."""
import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from tools.dashboard import voice_gateway as gateway
from tools.dashboard import voice_service_config as config


@pytest.fixture
def gateway_env(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TLS", "on")
    monkeypatch.setenv("VOICE_PUBLIC_PORT", "8443")
    monkeypatch.setenv("VOICE_DASHBOARD_PUBLIC_PORT", "443")


@pytest.mark.parametrize("profile,flag,valid", [
    ("", "false", True), ("voice", "true", True),
    ("beads,voice", "true", True), ("", "true", False),
    ("voice", "false", False),
])
def test_compose_modes(monkeypatch, profile, flag, valid):
    monkeypatch.setenv("VOICE_COMPOSE_MODE", "true")
    monkeypatch.setenv("COMPOSE_PROFILES", profile)
    monkeypatch.setenv("VOICE_SIDECAR_ENABLED", flag)
    if valid:
        config.validate_compose_mode()
        assert bool(config.meta()) is (flag == "true")
    else:
        with pytest.raises(ValueError):
            config.validate_compose_mode()


@pytest.mark.parametrize("origin,host,expected", [
    ("https://node.example", "node.example:8443", True),
    ("https://node.example:443", "node.example:8443", True),
    ("https://other.example", "node.example:8443", False),
    ("https://node.example:8080", "node.example:8443", False),
    ("https://node.example", "node.example:8082", False),
    ("http://node.example", "node.example:8443", False),
    ("", "node.example:8443", False),
    ("null", "node.example:8443", False),
    ("https://user@node.example", "node.example:8443", False),
])
def test_public_origin_contract(gateway_env, origin, host, expected):
    assert gateway.valid_origin(origin, host) is expected


def test_missing_cookie_rejected_even_when_dashboard_gate_disabled(gateway_env, monkeypatch):
    monkeypatch.setattr(gateway.unlock_routes, "verify_session_token", lambda token: None)
    with TestClient(gateway.app, base_url="https://node.example:8443") as client:
        with pytest.raises(WebSocketDisconnect) as failure:
            with client.websocket_connect(
                "wss://node.example:8443/ws/voice?bind=target", headers={"origin": "https://node.example"},
            ):
                pass
        assert failure.value.code == 4401


def test_authenticated_logins_get_distinct_buffer_owners(gateway_env, monkeypatch):
    owners = []
    monkeypatch.setattr(gateway.unlock_routes, "verify_session_token", lambda token: {"sid": token})

    async def protocol(websocket, **dependencies):
        owners.append(dependencies["buffer_owner"])
        await websocket.accept()
        await websocket.send_json({"owner": dependencies["buffer_owner"]})
        await websocket.receive_text()

    monkeypatch.setattr(gateway, "serve_voice", protocol)
    with TestClient(gateway.app, base_url="https://node.example:8443") as client:
        def headers(owner):
            return {"origin": "https://node.example", "cookie": f"{gateway.unlock_routes.SESSION_COOKIE}={owner}"}
        with client.websocket_connect("wss://node.example:8443/ws/voice?bind=same", headers=headers("alice")) as alice:
            assert alice.receive_json() == {"owner": "alice"}
            with client.websocket_connect("wss://node.example:8443/ws/voice?bind=same", headers=headers("bob")) as bob:
                assert bob.receive_json() == {"owner": "bob"}
                bob.send_text("done")
            alice.send_text("done")
    assert owners == ["alice", "bob"]


def test_concurrent_streams_keep_transcripts_and_disconnects_independent(gateway_env, monkeypatch):
    from types import SimpleNamespace
    from tools.dashboard import voice_buffer, voice_whisperlive, voice_transcription_settings
    clients = []

    class Transcriber:
        def __init__(self, **kwargs):
            self.on_final = kwargs["on_final"]
            self.ready = False
            clients.append(self)

        async def connect_and_wait_ready(self):
            self.ready = True

        def is_ready(self):
            return self.ready

        def is_unavailable(self):
            return False

        async def close(self):
            self.ready = False

    monkeypatch.setattr(gateway.unlock_routes, "verify_session_token", lambda token: {"sid": token})
    monkeypatch.setattr(gateway, "session_exists", lambda bind: True)
    monkeypatch.setattr(gateway.feature_flags, "is_enabled", lambda name: name == "voice.pipe_enabled")
    monkeypatch.setattr(voice_buffer, "MANAGER", voice_buffer.BufferManager())
    monkeypatch.setattr(voice_whisperlive, "WhisperLiveClient", Transcriber)
    monkeypatch.setattr(voice_transcription_settings, "resolve_transcription_config", lambda: SimpleNamespace(
        model="test", language="en", use_vad=True, no_speech_thresh=0.6, vad_threshold=0.5,
    ))

    def headers(owner):
        return {"origin": "https://node.example", "cookie": f"{gateway.unlock_routes.SESSION_COOKIE}={owner}"}

    def start(ws):
        ws.send_json({"type": "start"})
        while ws.receive_json().get("type") != "voice_state":
            pass

    with TestClient(gateway.app) as client:
        url = "wss://node.example:8443/ws/voice?bind=same"
        with client.websocket_connect(url, headers=headers("alice")) as alice:
            start(alice)
            with client.websocket_connect(url, headers=headers("bob")) as bob:
                start(bob)
                alice.portal.call(clients[0].on_final, "Alice private draft")
                assert alice.receive_json()["text"] == "Alice private draft"
                bob.portal.call(clients[1].on_final, "Bob private draft")
                assert bob.receive_json()["text"] == "Bob private draft"
                assert len(clients) == 2
            assert clients[0].ready, "Bob disconnect must not close Alice upstream"
            alice.portal.call(clients[0].on_final, "Alice continues")
            assert alice.receive_json()["text"] == "Alice continues"
        assert voice_buffer.MANAGER.get_text('["alice", "same"]') == "Alice private draft Alice continues"
        assert voice_buffer.MANAGER.get_text('["bob", "same"]') == "Bob private draft"
