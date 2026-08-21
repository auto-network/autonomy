from __future__ import annotations

import asyncio

import pytest

from tools.network.fleet_roster import enroll, kick
from tools.network.fleet_sync_channel import (
    FleetAuthenticator,
    FleetDirectServer,
    fleet_direct_connect,
)
from tools.network.idkit import KeyPair
from tools.network.relaykit.channel import HandshakeError
from tools.network.relaykit.direct import new_session_id


def test_fleet_channel_mutually_authenticates_and_encrypts_messages() -> None:
    async def run() -> None:
        root = KeyPair.generate()
        left = KeyPair.generate()
        right = KeyPair.generate()
        entries = [
            enroll(root, machine_pub=left.public_hex),
            enroll(root, machine_pub=right.public_hex),
        ]

        async def echo(_token: str, message: bytes, peer_pub: str) -> bytes:
            assert peer_pub == left.public_hex
            return message

        server = FleetDirectServer(
            FleetAuthenticator(
                right, root_pub=root.public_hex, roster_entries=lambda: entries
            ),
            echo,
        )
        port = await server.start()
        try:
            channel = await fleet_direct_connect(
                f"ws://127.0.0.1:{port}",
                authenticator=FleetAuthenticator(
                    left, root_pub=root.public_hex, roster_entries=lambda: entries
                ),
                expected_machine_pub=right.public_hex,
                session=new_session_id(),
            )
            async with channel:
                await channel.send_message(b"personal database transaction")
                assert await channel.recv_message() == b"personal database transaction"
        finally:
            await server.stop()
        assert server.connection_count == 0

    asyncio.run(run())

def test_fleet_channel_rejects_machine_outside_personal_root_roster() -> None:
    async def run() -> None:
        root = KeyPair.generate()
        authorized = KeyPair.generate()
        outsider = KeyPair.generate()
        entries = [enroll(root, machine_pub=authorized.public_hex)]

        async def echo(_token: str, message: bytes, _peer_pub: str) -> bytes:
            return message

        server = FleetDirectServer(
            FleetAuthenticator(
                authorized,
                root_pub=root.public_hex,
                roster_entries=lambda: entries,
            ),
            echo,
        )
        port = await server.start()
        try:
            with pytest.raises(HandshakeError, match="not active"):
                await fleet_direct_connect(
                    f"ws://127.0.0.1:{port}",
                    authenticator=FleetAuthenticator(
                        outsider,
                        root_pub=root.public_hex,
                        roster_entries=lambda: entries,
                    ),
                    expected_machine_pub=authorized.public_hex,
                    session=new_session_id(),
                )
        finally:
            await server.stop()

    asyncio.run(run())


def test_kick_closes_an_already_authenticated_fleet_channel() -> None:
    async def run() -> None:
        root = KeyPair.generate()
        left = KeyPair.generate()
        right = KeyPair.generate()
        entries = [
            enroll(root, machine_pub=left.public_hex),
            enroll(root, machine_pub=right.public_hex),
        ]

        async def echo(_token: str, message: bytes, _peer_pub: str) -> bytes:
            return message

        server = FleetDirectServer(
            FleetAuthenticator(
                right, root_pub=root.public_hex, roster_entries=lambda: entries
            ),
            echo,
        )
        port = await server.start()
        channel = await fleet_direct_connect(
            f"ws://127.0.0.1:{port}",
            authenticator=FleetAuthenticator(
                left, root_pub=root.public_hex, roster_entries=lambda: entries
            ),
            expected_machine_pub=right.public_hex,
            session=new_session_id(),
        )
        try:
            await channel.send_message(b"before kick")
            assert await channel.recv_message() == b"before kick"
            entries.append(kick(root, machine_pub=left.public_hex, seq=1))
            await channel.send_message(b"after kick")
            with pytest.raises(Exception):
                await asyncio.wait_for(channel.recv_message(), timeout=1.0)
        finally:
            await channel.close()
            await server.stop()
        assert server.connection_count == 0

    asyncio.run(run())
