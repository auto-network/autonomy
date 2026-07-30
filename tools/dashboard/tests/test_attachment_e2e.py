"""auto-lh57j: end-to-end attachment download on the real stack.

Stands up the real registry+relay subprocess and the real in-process
``TunnelConnector`` serving the grant handler, publishes a note with a large
non-image attachment, and downloads it as a fresh unauthenticated viewer
would: the real ``AttachmentDownloader`` reference driver pulls windows over
a real ``ViewerChannel`` (X25519 + AES-256-GCM), through the relay, from the
real attachment.fetch handler. Proves reconstruction + hash equality,
disconnect/resume by committed offset, and that an unauthorized reference
serves zero bytes.

The browser render (auto-1cs87) and the parent OPFS/cursor/export driver
(auto-2cmzd) are validated in their own agent-browser suites; this capstone
proves the server -> relay -> channel -> client data path and the abuse
bounds. The per-tunnel channel cap is unit-tested in test_relay_backpressure.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

import agents.design_db as design_db
from tools.dashboard import link_serving
from tools.graph import ops as graph_ops
from tools.graph import settings_ops
from tools.graph.schemas.network_identity import (
    NETWORK_LINK_GRANT_REVISION,
    NETWORK_LINK_GRANT_SET_ID,
)
from tools.network.idkit import KeyPair, Subject, canonical_json, issue_cert
from tools.network.registry.signing import sign_request
from tools.network.relaykit import attachment_download
from tools.network.relaykit.connector import TunnelConnector
from tools.network.relaykit.viewer import ViewerChannel

REPO = Path(__file__).resolve().parents[3]
ORG = "netorg"
ORG_UUID = "77777777-7777-4777-8777-777777777777"
ISO = "%Y-%m-%dT%H:%M:%SZ"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_registry(port: int, db: Path, log: Path) -> subprocess.Popen:
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "tools.network.registry",
         "--db", str(db), "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(REPO), env=env,
        stdout=open(log, "ab"), stderr=subprocess.STDOUT,
    )
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            if httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=1).status_code == 200:
                return proc
        except httpx.HTTPError:
            time.sleep(0.2)
    proc.kill()
    raise RuntimeError(f"registry did not start; log: {log.read_text()[-2000:]}")


def publish_note_link(port: int, root: KeyPair, note_id: str) -> str:
    with httpx.Client(base_url=f"http://127.0.0.1:{port}") as client:
        response = client.post("/v1/links", json=sign_request(
            root, "POST", "/v1/links",
            {"org": ORG_UUID, "target_uuid": note_id, "target_type": "note"},
            ts=int(time.time()),
        ))
        assert response.status_code == 201, response.text
        return response.json()["token"]


def cache_note_grant(token: str, note_id: str) -> None:
    settings_ops.add_setting(
        NETWORK_LINK_GRANT_SET_ID, NETWORK_LINK_GRANT_REVISION, token,
        {
            "token": token,
            "url": f"https://relay.auto.network/l/{token}",
            "target_uuid": note_id,
            "target_type": "note",
            "meta": {},
            "subject": {"kind": "operator", "id": "op-1"},
            "issued_at": time.strftime(ISO, time.gmtime()),
        },
        org=ORG,
    )


@pytest.fixture
def stack(tmp_path, monkeypatch):
    from tools.graph.db import GraphDB

    GraphDB.close_all_pooled()
    monkeypatch.setenv("GRAPH_DB", str(tmp_path / "graph.db"))
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    monkeypatch.setattr(design_db, "DB_PATH", tmp_path / "designs.db")
    monkeypatch.setattr(design_db, "_initialized", False)

    root = KeyPair.generate()
    session_key = KeyPair.generate()
    now = int(time.time())
    session_cert = issue_cert(
        root, session_key.public_hex, scope=("tunnel:serve",), org=ORG_UUID,
        subject=Subject("operator", "op-session-1"),
        not_before=now - 300, not_after=now + 86_400,
    )
    port = free_port()
    registry = start_registry(port, tmp_path / "registry.db", tmp_path / "registry.log")
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}") as client:
            resp = client.post("/v1/orgs", json=sign_request(
                root, "POST", "/v1/orgs",
                {"org_uuid": ORG_UUID, "root_pub": root.public_hex,
                 "recovery_policy": "none"}, ts=now))
            assert resp.status_code == 201, resp.text
        connector = TunnelConnector(
            f"ws://127.0.0.1:{port}", ORG_UUID, session_key, session_cert,
            handler=link_serving.make_grant_handler(ORG),
            min_backoff=0.1, max_backoff=1.0,
        )
        yield {"port": port, "root": root, "root_pub": root.public_hex,
               "connector": connector}
    finally:
        registry.terminate()
        registry.wait(timeout=5)
        GraphDB.close_all_pooled()


def _note_with_attachment(tmp_path, data: bytes):
    path = tmp_path / "payload.bin"
    path.write_bytes(data)
    note = graph_ops.create_note(
        "![a]({1})", title="E2E", attachments=[str(path)], org=ORG)
    return note["id"], note["attachments"][0]["id"]


async def _fetch_manifest(port: int, token: str, root_pub: str) -> list:
    channel = await ViewerChannel.connect(
        f"ws://127.0.0.1:{port}", token, root_pub=root_pub, org=ORG_UUID)
    async with channel:
        await channel.send_message(canonical_json({"op": "fetch", "v": 1}))
        served = await channel.recv_message()
    header, _, _ = served.partition(b"\n")
    return json.loads(header)["content"]["attachments"]


def _make_fetch_window(port: int, token: str, root_pub: str, *, drop=None):
    async def fetch_window(request):
        channel = await ViewerChannel.connect(
            f"ws://127.0.0.1:{port}", token, root_pub=root_pub, org=ORG_UUID)

        async def gen():
            try:
                await channel.send_message(canonical_json(request))
                async for message, final in channel.recv_message_stream():
                    if drop is not None:
                        drop["seen"] += 1
                        if drop["seen"] == drop["at"] and not drop["done"]:
                            drop["done"] = True
                            raise ConnectionError("forced mid-window drop")
                    yield message
                    if final:
                        return
            finally:
                await channel.close()

        return gen()

    return fetch_window


async def _connected(connector):
    task = asyncio.create_task(connector.run())
    await asyncio.wait_for(connector.connected.wait(), timeout=15)
    return task


async def _stop(connector, task):
    connector.stop()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


def test_end_to_end_download_reconstructs_and_hashes(stack, tmp_path):
    data = os.urandom(9 * 1024 * 1024 + 4242)  # spans two 8 MiB windows
    note_id, _ = _note_with_attachment(tmp_path, data)
    token = publish_note_link(stack["port"], stack["root"], note_id)
    cache_note_grant(token, note_id)

    async def run():
        task = await _connected(stack["connector"])
        try:
            manifest = await _fetch_manifest(stack["port"], token, stack["root_pub"])
            assert len(manifest) == 1
            entry = manifest[0]
            assert entry["total_size"] == len(data)
            assert entry["raw_sha256"] == hashlib.sha256(data).hexdigest()

            sink = attachment_download.MemoryAttachmentSink()
            cursors = attachment_download.MemoryCursorStore()
            downloader = attachment_download.AttachmentDownloader(
                entry, link_token=token, note_id=note_id, sink=sink,
                cursors=cursors,
                fetch_window=_make_fetch_window(
                    stack["port"], token, stack["root_pub"]),
            )
            result = await downloader.run()
            assert result.status == "complete"
            assert sink.data == data
            assert hashlib.sha256(sink.data).hexdigest() == entry["raw_sha256"]
        finally:
            await _stop(stack["connector"], task)

    asyncio.run(run())


def test_end_to_end_disconnect_then_resume(stack, tmp_path):
    data = os.urandom(9 * 1024 * 1024 + 17)
    note_id, _ = _note_with_attachment(tmp_path, data)
    token = publish_note_link(stack["port"], stack["root"], note_id)
    cache_note_grant(token, note_id)

    async def run():
        task = await _connected(stack["connector"])
        try:
            entry = (await _fetch_manifest(
                stack["port"], token, stack["root_pub"]))[0]
            sink = attachment_download.MemoryAttachmentSink()
            cursors = attachment_download.MemoryCursorStore()
            drop = {"seen": 0, "at": 3, "done": False}

            def build(fetch_window):
                return attachment_download.AttachmentDownloader(
                    entry, link_token=token, note_id=note_id, sink=sink,
                    cursors=cursors, fetch_window=fetch_window)

            # First run drops mid-window after two committed chunks.
            with pytest.raises(attachment_download.AttachmentDisconnected):
                await build(_make_fetch_window(
                    stack["port"], token, stack["root_pub"], drop=drop)).run()
            assert drop["done"]
            cursor = cursors.get(f"{attachment_download.cursor_id(token)}:{entry['ref']}")
            assert cursor["committed_offset"] == 2 * 1024 * 1024  # only whole chunks
            assert sink.size == 2 * 1024 * 1024

            # Resume on a fresh channel completes from the committed offset.
            result = await build(_make_fetch_window(
                stack["port"], token, stack["root_pub"])).run()
            assert result.status == "complete"
            assert sink.data == data
            assert hashlib.sha256(sink.data).hexdigest() == entry["raw_sha256"]
        finally:
            await _stop(stack["connector"], task)

    asyncio.run(run())


def test_end_to_end_unauthorized_ref_serves_zero_bytes(stack, tmp_path):
    # A note the viewer is NOT granted, with its own attachment.
    other_id, other_ref = _note_with_attachment(tmp_path, os.urandom(1024))
    note_id, _ = _note_with_attachment(tmp_path, os.urandom(1024))
    token = publish_note_link(stack["port"], stack["root"], note_id)
    cache_note_grant(token, note_id)

    async def run():
        task = await _connected(stack["connector"])
        try:
            channel = await ViewerChannel.connect(
                f"ws://127.0.0.1:{stack['port']}", token,
                root_pub=stack["root_pub"], org=ORG_UUID)
            body_frames = 0
            error = None
            async with channel:
                await channel.send_message(canonical_json({
                    "v": 1, "op": "attachment.fetch",
                    "ref": other_ref, "offset": 0, "length": 8 * 1024 * 1024}))
                async for message, final in channel.recv_message_stream():
                    if message[:1] == b"{":
                        error = json.loads(message)
                    else:
                        body_frames += 1
                    if final:
                        break
            assert body_frames == 0                     # zero bytes served
            assert error is not None and error["op"] == "error"
            assert error["code"] == "not_authorized"    # foreign ref refused
        finally:
            await _stop(stack["connector"], task)

    asyncio.run(run())
