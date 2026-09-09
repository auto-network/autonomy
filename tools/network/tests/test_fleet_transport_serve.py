"""Real fleet auth and record exchange on paired message carriers."""

import asyncio

import pytest

from tools.network import clock
from tools.network.fleet_roster import enroll, kick
from tools.network.fleet_sync_channel import (
    FleetAuthenticator, authenticate_fleet_transport, serve_fleet_transport,
)
from tools.network.idkit import KeyPair
from tools.network.relaykit.channel import HandshakeError


class Carrier:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.other = None

    async def send(self, message):
        self.other.incoming.put_nowait(message)

    async def recv(self):
        return await self.incoming.get()

    async def close(self, **kwargs):
        self.other.incoming.put_nowait(None)


def setup_pair(*, outsider=False):
    root, left, right = (KeyPair.generate() for _ in range(3))
    entries = [enroll(root, machine_pub=key.public_hex)
               for key in ((right,) if outsider else (left, right))]
    # An outsider can claim membership locally; the server's roster must
    # independently refuse it, rather than relying on client-side checks.
    client_entries = [enroll(root, machine_pub=key.public_hex)
                      for key in (left, right)]
    client = FleetAuthenticator(left, root_pub=root.public_hex,
                                roster_entries=lambda: client_entries)
    server = FleetAuthenticator(right, root_pub=root.public_hex,
                                roster_entries=lambda: entries)
    a, b = Carrier(), Carrier()
    a.other, b.other = b, a
    return root, left, entries, client, server, a, b


def test_shared_server_and_client_roundtrip_and_live_revocation(monkeypatch):
    monkeypatch.setattr(clock, "AUTHORIZE_CACHE_TTL_S", 0.0)

    async def scenario():
        root, left, entries, client, server, a, b = setup_pair()
        calls = []

        async def handler(token, message, peer):
            calls.append((token, message, peer))
            return message

        task = asyncio.create_task(serve_fleet_transport(
            token="paired", recv=b.recv, send=b.send, close=b.close,
            handler=handler, authenticator=server,
        ))
        try:
            channel = await authenticate_fleet_transport(
                a, authenticator=client, expected_machine_pub=server.machine_pub,
                session="paired",
            )
            for message in (b"one", b"two"):
                await channel.send_message(message)
                assert await asyncio.wait_for(channel.recv_message(), 1) == message
            entries.append(kick(root, machine_pub=left.public_hex, seq=1))
            await channel.send_message(b"must never reach handler")
            with pytest.raises(HandshakeError):
                await asyncio.wait_for(task, 1)
            assert calls == [("paired", value, left.public_hex)
                             for value in (b"one", b"two")]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("outsider", [False, True])
def test_rejected_peer_never_invokes_shared_server_handler(outsider):
    async def scenario():
        _, _, _, client, server, a, b = setup_pair(outsider=outsider)
        calls = []

        async def serve():
            try:
                await serve_fleet_transport(
                    token="refused", recv=b.recv, send=b.send, close=b.close,
                    handler=lambda *args: calls.append(args), authenticator=server,
                )
            finally:
                await b.close()  # The adapter owns the connection lifetime.

        task = asyncio.create_task(serve())
        try:
            with pytest.raises((HandshakeError, ValueError, TypeError)):
                await asyncio.wait_for(authenticate_fleet_transport(
                    a, authenticator=client,
                    expected_machine_pub=(server.machine_pub if outsider
                                          else client.machine_pub),
                    session="refused",
                ), 1)
            result = await asyncio.wait_for(
                asyncio.gather(task, return_exceptions=True), 1,
            )
            if outsider:
                assert isinstance(result[0], HandshakeError)
            else:
                assert result == [None]
            assert calls == []
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
