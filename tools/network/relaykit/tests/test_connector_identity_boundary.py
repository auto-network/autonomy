"""The connector sends each serving certificate only in its intended context."""

from __future__ import annotations

import asyncio
import json
import time

from tools.network.idkit import DelegationCert, KeyPair, Subject, issue_cert
from tools.network.relaykit.channel import build_client_hello
from tools.network.relaykit.connector import TunnelConnector
from tools.network.relaykit.frames import FRAME_DATA, VIEWER_KIND_RECORD, split_viewer_message


ORG = "11111111-1111-4111-8111-111111111111"
PERSONA = "ab" * 32


def _credential_pair():
    root = KeyPair.generate()
    child = KeyPair.generate()
    now = int(time.time())
    common = dict(
        scope=("tunnel:serve",),
        org=ORG,
        not_before=now - 10,
        not_after=now + 30 * 86400,
    )
    registry_cert = issue_cert(
        root, child.public_hex,
        subject=Subject("persona", PERSONA), **common,
    )
    viewer_cert = issue_cert(
        root, child.public_hex,
        subject=Subject("operator", child.public_hex), **common,
    )
    return child, registry_cert, viewer_cert


def test_actual_registry_and_viewer_hello_bytes_use_distinct_certificates():
    child, registry_cert, viewer_cert = _credential_pair()
    connector = TunnelConnector(
        "ws://relay.invalid", ORG, child, registry_cert,
        channel_cert=viewer_cert,
    )

    class FakeRegistrySocket:
        def __init__(self):
            self.sent = []

        async def send(self, value):
            self.sent.append(value)

        async def recv(self):
            return json.dumps({"ok": True})

    async def exercise():
        registry_socket = FakeRegistrySocket()
        await connector._handshake(registry_socket)

        _viewer_eph, client_hello = build_client_hello()
        incoming = asyncio.Queue()
        incoming.put_nowait(client_hello)
        incoming.put_nowait(None)
        emitted = []

        async def send_frame(frame_type, channel_id, payload=b""):
            emitted.append((frame_type, channel_id, payload))

        await connector._serve_channel(
            b"c" * 16, "f" * 32, incoming, send_frame, lambda _channel: None,
        )
        return registry_socket.sent, emitted

    registry_bytes, emitted = asyncio.run(exercise())

    # Positive side of the split: the actual registry hello carries the
    # persona-bearing certificate and therefore the persona routing key.
    assert len(registry_bytes) == 1
    registry_wire = registry_bytes[0]
    assert json.loads(registry_wire)["cert"] == registry_cert.to_json().decode("ascii")
    assert PERSONA in registry_wire

    # Load-bearing privacy assertion: enumerate the actual payload emitted by
    # TunnelConnector for the viewer SERVER_HELLO, after viewer-kind framing.
    viewer_payloads = [
        payload for frame_type, _channel_id, payload in emitted
        if frame_type == FRAME_DATA
    ]
    assert viewer_payloads
    kind, server_hello_bytes = split_viewer_message(viewer_payloads[0])
    assert kind == VIEWER_KIND_RECORD
    assert PERSONA.encode("ascii") not in server_hello_bytes

    server_hello = json.loads(server_hello_bytes)
    assert server_hello["cert"] == viewer_cert.to_json().decode("ascii")
    on_wire_cert = DelegationCert.from_json(server_hello["cert"])
    assert on_wire_cert.subject.kind == "operator"
    assert on_wire_cert.subject.id == child.public_hex
