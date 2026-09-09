"""Exact fleet authentication on a carrier, without a WebSocket dial.

Direct-socket integration stays covered by test_fleet_sync_channel. These
controls exercise the shared handshake with real signatures and AEAD records.
"""

import asyncio
import json

import pytest

from tools.network.fleet_roster import enroll
from tools.network.fleet_sync_channel import (
    FleetAuthenticator,
    authenticate_fleet_transport,
)
from tools.network.idkit import KeyPair
from tools.network.relaykit.channel import ChannelCrypto, HandshakeError
from tools.network.relaykit.frames import VIEWER_KIND_RECORD, tag_viewer_message


def _auth_pair():
    root, left, right = (KeyPair.generate() for _ in range(3))
    entries = [enroll(root, machine_pub=key.public_hex) for key in (left, right)]
    return tuple(
        FleetAuthenticator(key, root_pub=root.public_hex,
                           roster_entries=lambda: entries)
        for key in (left, right)
    )


class _EchoTransport:
    """Message carrier with an actual fleet-authenticated server endpoint."""

    def __init__(self, server, session):
        self.server, self.session = server, session
        self.incoming = asyncio.Queue()
        self.crypto = None
        self.close_count = 0
        self.client_hello = None

    async def send(self, payload):
        if self.crypto is None:
            self.client_hello = payload
            _, private, hello, transcript = self.server.accept_client(
                payload, session=self.session,
            )
            self.crypto = ChannelCrypto.server(
                private, json.loads(payload)["eph_pub"], transcript,
            )
            self.incoming.put_nowait(tag_viewer_message(VIEWER_KIND_RECORD, hello))
        else:
            message = self.crypto.open_record(payload)
            if message is not None:
                for record in self.crypto.seal_message(message):
                    self.incoming.put_nowait(
                        tag_viewer_message(VIEWER_KIND_RECORD, record)
                    )

    async def recv(self):
        return await self.incoming.get()

    async def close(self):
        self.close_count += 1


def test_shared_handshake_exchanges_records_and_refreshes_crypto():
    async def scenario():
        client, server = _auth_pair()
        transports = []
        for _ in range(2):
            transport = _EchoTransport(server, "same-peer-new-carrier")
            channel = await authenticate_fleet_transport(
                transport, authenticator=client,
                expected_machine_pub=server.machine_pub,
                session=transport.session,
            )
            async with channel:
                for payload in (b"first record", b"second record"):
                    await channel.send_message(payload)
                    assert await channel.recv_message() == payload
            assert transport.close_count == 1
            transports.append(transport)
        assert json.loads(transports[0].client_hello)["eph_pub"] != (
            json.loads(transports[1].client_hello)["eph_pub"]
        )
        # A fresh connection starts its own sequence and key state, rather
        # than inheriting either from the previous carrier.
        assert transports[0].crypto is not transports[1].crypto

    asyncio.run(scenario())


def test_shared_handshake_rejects_wrong_expected_machine_and_closes():
    async def scenario():
        client, server = _auth_pair()
        transport = _EchoTransport(server, "wrong-peer")
        with pytest.raises(HandshakeError):
            await authenticate_fleet_transport(
                transport, authenticator=client,
                expected_machine_pub=client.machine_pub, session=transport.session,
            )
        assert transport.close_count == 1

    asyncio.run(scenario())


def test_cancelled_handshake_closes_carrier_without_returning_channel():
    async def scenario():
        client, server = _auth_pair()
        transport = _EchoTransport(server, "cancelled")
        receiving = asyncio.Event()

        async def blocked_recv():
            receiving.set()
            await asyncio.Event().wait()

        transport.recv = blocked_recv
        task = asyncio.create_task(authenticate_fleet_transport(
            transport, authenticator=client,
            expected_machine_pub=server.machine_pub, session=transport.session,
        ))
        await asyncio.wait_for(receiving.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert transport.close_count == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("reply", ["text is not a binary hello", b"truncated"])
def test_invalid_reply_closes_carrier(reply):
    async def scenario():
        client, server = _auth_pair()
        transport = _EchoTransport(server, "invalid")

        async def invalid_recv():
            return reply

        transport.recv = invalid_recv
        with pytest.raises((HandshakeError, ValueError)):
            await authenticate_fleet_transport(
                transport, authenticator=client,
                expected_machine_pub=server.machine_pub, session=transport.session,
            )
        assert transport.close_count == 1

    asyncio.run(scenario())
