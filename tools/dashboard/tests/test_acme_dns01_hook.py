from __future__ import annotations

import asyncio
import json
import socket

from tools.dashboard.acme_dns01_hook import Dns01HookServer


class _Client:
    def __init__(self):
        self.calls = []

    def present(self, order, value):
        self.calls.append(("present", order, value))
        return {"name": "_acme-challenge.p.serve.auto.network",
                "expires_at": 1700000600}

    def cleanup(self, order, value):
        self.calls.append(("cleanup", order, value))


def _request(path, payload):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(str(path))
        sock.sendall((json.dumps(payload) + "\n").encode())
        data = b""
        while b"\n" not in data:
            data += sock.recv(4096)
    return json.loads(data.split(b"\n", 1)[0])


def test_order_bound_socket_presents_and_cleans_only_its_value(tmp_path):
    async def scenario():
        client = _Client()
        path = tmp_path / "dns01.sock"
        async with Dns01HookServer(client, "certbot-run-7", path):
            shown = await asyncio.to_thread(
                _request, path, {"action": "present", "value": "txt-value"})
            cleaned = await asyncio.to_thread(
                _request, path, {"action": "cleanup", "value": "txt-value"})
            assert shown == {"ok": True,
                             "name": "_acme-challenge.p.serve.auto.network",
                             "expires_at": 1700000600}
            assert cleaned == {"ok": True}
        assert client.calls == [
            ("present", "certbot-run-7", "txt-value"),
            ("cleanup", "certbot-run-7", "txt-value"),
        ]
        assert not path.exists()

    asyncio.run(scenario())


def test_socket_refuses_extra_fields_and_unknown_actions(tmp_path):
    async def scenario():
        client = _Client()
        path = tmp_path / "dns01.sock"
        async with Dns01HookServer(client, "certbot-run-7", path):
            extra = await asyncio.to_thread(
                _request, path,
                {"action": "present", "value": "txt", "org": "other"})
            unknown = await asyncio.to_thread(
                _request, path, {"action": "delete-all", "value": "txt"})
            assert extra == {"ok": False, "error": "invalid request"}
            assert unknown == {"ok": False, "error": "invalid request"}
        assert client.calls == []

    asyncio.run(scenario())
