"""Real machine Settings + loopback socket + inherited-pipe custody boundary."""
import asyncio
import hashlib
import json
import os
import subprocess
import sys

import pytest

from tools.dashboard import connector_key_resolution as resolver
from tools.dashboard import link_channel_key, link_serving
from tools.graph import settings_ops
from tools.network.idkit import KeyPair

ORG_ID = "2d4b90cb-0000-4000-8000-000000000000"
TOKEN = "ab" * 16


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    key = KeyPair.generate()
    state = {"key": key, "active": True}
    def grant(token, *, org):
        if token == TOKEN and org == "autonomy" and state["active"]:
            return {"target_type": "present", "channel_pub": state["key"].public_hex}
    monkeypatch.setattr(link_serving, "check_grant", grant)
    monkeypatch.setattr(link_channel_key, "channel_key_for", lambda token, org: state["key"])
    return state


def test_machine_hash_only_and_protected_write(setup):
    bootstrap = resolver.register(os.getpid(), ORG_ID, "autonomy", "commit")
    payload = resolver.record(ORG_ID)["payload"]
    assert payload["token_hash"] == hashlib.sha256(bootstrap["auth"].encode()).hexdigest()
    assert bootstrap["auth"] not in json.dumps(payload)
    with pytest.raises(settings_ops.ProtectedSettingError):
        settings_ops.upsert_by_key(resolver.SET_ID, 1, ORG_ID, payload, org="machine")
    resolver.retire(os.getpid())
    assert resolver.record(ORG_ID) is None


def test_socket_resolves_live_holder_and_adoption_preserves_bearer(setup):
    bootstrap = resolver.register(os.getpid(), ORG_ID, "autonomy", "commit")
    fetch = resolver.client(bootstrap)
    assert asyncio.run(fetch(TOKEN)).public_hex == setup["key"].public_hex
    original = resolver.record(ORG_ID)["payload"]["token_hash"]
    assert resolver.adopt(ORG_ID, "autonomy", os.getpid())
    assert resolver.record(ORG_ID)["payload"]["token_hash"] == original
    setup["key"] = KeyPair.generate()
    assert asyncio.run(fetch(TOKEN)).public_hex == setup["key"].public_hex
    setup["active"] = False
    with pytest.raises(PermissionError):
        asyncio.run(fetch(TOKEN))


@pytest.mark.parametrize("change", ["auth", "protocol", "scope", "birth", "absent-key"])
def test_refusals(setup, change, monkeypatch):
    request = {**resolver.register(os.getpid(), ORG_ID, "autonomy", "commit"), "token": TOKEN}
    if change == "auth":
        request["auth"] = "wrong"
    elif change == "protocol":
        request["protocol_version"] = 999
    elif change in ("scope", "birth"):
        payload = resolver.record(ORG_ID)["payload"]
        payload["organization" if change == "scope" else "process_start"] = "wrong"
        resolver._write(ORG_ID, payload)
    else:
        monkeypatch.setattr(link_serving, "check_grant", lambda *a, **kw: {"target_type": "present"})
    with pytest.raises(PermissionError):
        resolver.resolve(request)


def test_revoke_during_open_releases_nothing(setup, monkeypatch):
    request = {**resolver.register(os.getpid(), ORG_ID, "autonomy", "commit"), "token": TOKEN}
    def open_key(*args):
        setup["active"] = False
        return setup["key"]
    monkeypatch.setattr(link_channel_key, "channel_key_for", open_key)
    with pytest.raises(PermissionError):
        resolver.resolve(request)


def test_real_child_receives_only_pipe_bearer_and_link_key(setup):
    read_fd, write_fd = os.pipe()
    code = """
import asyncio, sys
from tools.dashboard.connector_key_resolution import read_bootstrap, client
bootstrap = read_bootstrap(int(sys.argv[1]))
key = asyncio.run(client(bootstrap)(sys.argv[2]))
print(key.public_hex, flush=True)
"""
    child = subprocess.Popen([sys.executable, "-c", code, str(read_fd), TOKEN],
                             pass_fds=(read_fd,), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    os.close(read_fd)
    try:
        bootstrap = resolver.register(child.pid, ORG_ID, "autonomy", "commit")
        os.write(write_fd, json.dumps(bootstrap).encode() + b"\n")
        os.close(write_fd)
        out, err = child.communicate(timeout=15)
        assert child.returncode == 0, err.decode()
        assert out.decode().strip() == setup["key"].public_hex
        assert not resolver.adopt(ORG_ID, "autonomy", child.pid)
        with pytest.raises(PermissionError):
            resolver.resolve({**bootstrap, "token": TOKEN})
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        resolver.retire(child.pid)


def test_resolver_failure_closes_open_without_certificate_fallback(monkeypatch):
    from types import SimpleNamespace
    from tools.network.relaykit import connector as wire
    from tools.network.relaykit.frames import FRAME_CLOSE
    sent, dropped = [], []
    async def unavailable(token):
        raise PermissionError("vault unavailable")
    async def forbidden(*args, **kwargs):
        pytest.fail("a failed resolver must not reach the certificate handshake")
    async def send(*args):
        sent.append(args)
    monkeypatch.setattr(wire, "serve_channel", forbidden)
    fake = SimpleNamespace(_link_key_for=unavailable, _publisher=None)
    asyncio.run(wire.TunnelConnector._serve_channel(
        fake, b"channel", TOKEN, asyncio.Queue(), send, dropped.append))
    assert sent == [(FRAME_CLOSE, b"channel")]
    assert dropped == [b"channel"]


def test_adoption_rejects_missing_or_changed_process_record(setup):
    assert not resolver.adopt(ORG_ID, "autonomy", os.getpid())
    bootstrap = resolver.register(os.getpid(), ORG_ID, "autonomy", "commit")
    assert not resolver.adopt(ORG_ID, "other-org", os.getpid())
    row = resolver.record(ORG_ID)["payload"]
    row["process_start"] = "previous-process"
    resolver._write(ORG_ID, row)
    assert not resolver.adopt(ORG_ID, "autonomy", os.getpid())
    with pytest.raises(PermissionError):
        resolver.resolve({**bootstrap, "token": TOKEN})


def test_replacement_revokes_old_bearer(setup):
    old = resolver.register(os.getpid(), ORG_ID, "autonomy", "old-commit")
    new = resolver.register(os.getpid(), ORG_ID, "autonomy", "new-commit")
    assert old["auth"] != new["auth"]
    with pytest.raises(PermissionError):
        resolver.resolve({**old, "token": TOKEN})
    assert resolver.resolve({**new, "token": TOKEN})["seed"] == setup["key"].private_hex


def test_wrong_public_key_or_cold_holder_releases_nothing(setup, monkeypatch):
    fetch = resolver.client(resolver.register(os.getpid(), ORG_ID, "autonomy", "commit"))
    monkeypatch.setattr(link_channel_key, "channel_key_for", lambda *args: KeyPair.generate())
    with pytest.raises(PermissionError):
        asyncio.run(fetch(TOKEN))
    def cold(*args):
        raise link_channel_key.ChannelKeyUnavailable("no_key_holder")
    monkeypatch.setattr(link_channel_key, "channel_key_for", cold)
    with pytest.raises(PermissionError):
        asyncio.run(fetch(TOKEN))
