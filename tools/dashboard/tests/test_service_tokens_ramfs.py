"""The dispatcher's and voice gateway's scoped dashboard tokens live in the
ramfs key cache, never the data volume (auto-es7ja): both are re-minted at
every start, so nothing needs them to persist."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture
def ramfs_ok(monkeypatch):
    from tools.network.storagekit import memory_cache

    monkeypatch.setattr(memory_cache, "assert_memory_backed", lambda path, **k: None)


def test_the_dispatcher_token_is_minted_into_the_key_cache(tmp_path, monkeypatch, ramfs_ok):
    from tools.dashboard import server
    from tools.dashboard.dao import auth_db

    monkeypatch.setattr(auth_db, "insert_scoped_service_token", lambda *a, **k: None)
    target = tmp_path / "keycache" / "dispatcher" / "token"
    monkeypatch.setenv("AUTONOMY_DISPATCH_TOKEN_FILE", str(target))
    legacy = tmp_path / ".dispatch_token"
    legacy.write_text("old plaintext on the data volume")
    monkeypatch.setattr(server, "_LEGACY_DISPATCHER_TOKEN_FILE", legacy)
    server._ensure_dispatcher_service_token()
    assert len(target.read_text()) > 30
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert not legacy.exists()


def test_a_key_cache_that_is_not_ramfs_writes_no_token(tmp_path, monkeypatch, caplog):
    from tools.dashboard import server
    from tools.dashboard.dao import auth_db

    monkeypatch.setattr(auth_db, "insert_scoped_service_token", lambda *a, **k: None)
    target = tmp_path / "keycache" / "dispatcher" / "token"
    monkeypatch.setenv("AUTONOMY_DISPATCH_TOKEN_FILE", str(target))
    monkeypatch.setattr(server, "_LEGACY_DISPATCHER_TOKEN_FILE", tmp_path / ".dispatch_token")
    server._ensure_dispatcher_service_token()            # never raises at startup
    assert not target.exists()
    assert "could not be written to the ramfs key cache" in caplog.text


def test_the_voice_token_is_minted_into_its_key_cache_child(tmp_path, monkeypatch, ramfs_ok):
    from tools.dashboard import host_release, voice_service_token
    from tools.dashboard.dao import auth_db

    monkeypatch.setattr(auth_db, "insert_scoped_service_token", lambda *a, **k: None)
    monkeypatch.setenv("AUTONOMY_KEYCACHE_MOUNT", str(tmp_path / "keycache"))
    legacy = tmp_path / "voice-service.token"
    legacy.write_text("old")
    monkeypatch.setattr(voice_service_token, "LEGACY_TOKEN_FILE", legacy)
    voice_service_token.provision()
    token = host_release.release_dir("voice") / "token"
    assert len(token.read_text()) > 30
    assert stat.S_IMODE(token.stat().st_mode) == 0o600
    assert not legacy.exists()


def test_the_readers_look_in_the_key_cache(tmp_path, monkeypatch):
    from tools.graph import harness_credentials

    monkeypatch.delenv("AUTONOMY_DISPATCH_TOKEN_FILE", raising=False)
    monkeypatch.setenv("AUTONOMY_KEYCACHE_MOUNT", "/run/autonomy-keycache")
    assert harness_credentials.dispatch_token_path() == "/run/autonomy-keycache/dispatcher/token"
    source = (REPO / "tools/dashboard/voice_gateway.py").read_text()
    assert '"/run/voice-secrets/token"' in source and "voice-service.token" not in source
    assert '".dispatch_token"' not in (REPO / "agents/dispatcher.py").read_text()


def test_compose_gives_the_dispatcher_the_key_cache_and_voice_only_its_child():
    services = yaml.safe_load((REPO / "docker-compose.yml").read_text())["services"]
    dispatcher_binds = [v for v in services["dispatcher"]["volumes"]
                        if isinstance(v, dict) and v.get("type") == "bind"]
    assert dispatcher_binds == [{"type": "bind", "source": "/run/autonomy-keycache",
                                 "target": "/run/autonomy-keycache", "read_only": True,
                                 "bind": {"propagation": "rslave", "create_host_path": True}}]
    assert "/var/run/docker.sock:/var/run/docker.sock" in services["dispatcher"]["volumes"]
    voice_binds = [v for v in services["voice-gateway"]["volumes"]
                   if isinstance(v, dict) and v.get("type") == "bind"]
    assert voice_binds == [{"type": "bind", "source": "/run/autonomy-keycache/voice",
                            "target": "/run/voice-secrets", "read_only": True,
                            "bind": {"create_host_path": False}}]
    assert services["voice-gateway"]["environment"]["VOICE_SERVICE_TOKEN_FILE"] == \
        "/run/voice-secrets/token"
