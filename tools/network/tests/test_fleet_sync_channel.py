from __future__ import annotations

import asyncio
import time

import pytest

from tools.network import clock as _clock
from tools.network.clock import FLEET_RUNTIME_DELEGATION_TTL_SECONDS
from tools.network.fleet_roster import enroll, kick
from tools.network.fleet_sync_channel import (
    FleetAuthenticator,
    FleetDirectServer,
    fleet_direct_connect,
)
from tools.network.idkit import KeyPair, Subject, issue_cert
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


def test_kick_closes_an_already_authenticated_fleet_channel(monkeypatch) -> None:
    # A kick lands on an open channel within AUTHORIZE_CACHE_TTL_S (the
    # roster is cached per stream, see authorize()). Pin the window to zero
    # here to assert the enforcement path itself, not the bound.
    from tools.network import fleet_sync_channel
    monkeypatch.setattr(_clock, "AUTHORIZE_CACHE_TTL_S", 0.0)

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


def test_fleet_channel_uses_machine_signed_process_delegations() -> None:
    async def run() -> None:
        root = KeyPair.generate()
        left_machine = KeyPair.generate()
        right_machine = KeyPair.generate()
        left_entry = enroll(root, machine_pub=left_machine.public_hex)
        right_entry = enroll(root, machine_pub=right_machine.public_hex)
        entries = [left_entry, right_entry]
        now = int(time.time())
        org = f"personal:{root.public_hex}"

        def runtime(machine, entry):
            process = KeyPair.generate()
            cert = issue_cert(
                machine,
                process.public_hex,
                scope=["fleet:sync"],
                org=org,
                subject=Subject(kind="machine", id=entry.machine_id),
                not_before=now - 30,
                not_after=now + 300,
            )
            return FleetAuthenticator(
                process,
                root_pub=root.public_hex,
                roster_entries=lambda: entries,
                roster_machine_pub=machine.public_hex,
                delegation_cert=cert,
                require_delegation=True,
            )

        async def echo(_token: str, message: bytes, peer_pub: str) -> bytes:
            assert peer_pub == left_machine.public_hex
            return message

        server = FleetDirectServer(runtime(right_machine, right_entry), echo)
        port = await server.start()
        try:
            channel = await fleet_direct_connect(
                f"ws://127.0.0.1:{port}",
                authenticator=runtime(left_machine, left_entry),
                expected_machine_pub=right_machine.public_hex,
                session=new_session_id(),
            )
            async with channel:
                await channel.send_message(b"delegated")
                assert await channel.recv_message() == b"delegated"

            with pytest.raises(Exception):
                await fleet_direct_connect(
                    f"ws://127.0.0.1:{port}",
                    authenticator=FleetAuthenticator(
                        left_machine,
                        root_pub=root.public_hex,
                        roster_entries=lambda: entries,
                    ),
                    expected_machine_pub=right_machine.public_hex,
                    session=new_session_id(),
                )
        finally:
            await server.stop()

    asyncio.run(run())


def test_fleet_channel_refuses_overlong_process_delegation() -> None:
    root = KeyPair.generate()
    machine = KeyPair.generate()
    entry = enroll(root, machine_pub=machine.public_hex)
    process = KeyPair.generate()
    now = int(time.time())
    cert = issue_cert(
        machine,
        process.public_hex,
        scope=["fleet:sync"],
        org=f"personal:{root.public_hex}",
        subject=Subject(kind="machine", id=entry.machine_id),
        not_before=now - 30,
        # Exceed the delegation TTL bound by a day. Pinned to the live constant
        # (raised to 30 days in d0d6983af, matched to the serve-cert) so this
        # test tracks the bound instead of a stale hard-coded hour count.
        not_after=now + FLEET_RUNTIME_DELEGATION_TTL_SECONDS + (24 * 60 * 60),
    )
    auth = FleetAuthenticator(
        process,
        root_pub=root.public_hex,
        roster_entries=lambda: [entry],
        roster_machine_pub=machine.public_hex,
        delegation_cert=cert,
        require_delegation=True,
    )

    with pytest.raises(HandshakeError, match="TTL bound"):
        auth.build_client_hello(new_session_id())


def test_authorize_caches_the_resolved_roster_within_the_ttl(monkeypatch):
    """authorize() runs per served operation; the roster must be re-derived
    at most once per AUTHORIZE_CACHE_TTL_S, and a kick must still land once
    the window expires."""
    from tools.network import fleet_roster, fleet_sync_channel
    from tools.network.idkit import KeyPair
    from tools.network.relaykit.channel import HandshakeError

    root = KeyPair.generate()
    peer = KeyPair.generate()
    me = KeyPair.generate()
    entries = [fleet_roster.enroll(root, machine_pub=peer.public_hex),
               fleet_roster.enroll(root, machine_pub=me.public_hex)]
    calls = {"n": 0}

    def roster():
        calls["n"] += 1
        return list(entries)

    auth = fleet_sync_channel.FleetAuthenticator(
        me, root_pub=root.public_hex, roster_entries=roster,
    )
    clock = {"t": 100.0}
    monkeypatch.setattr(fleet_sync_channel.time, "monotonic", lambda: clock["t"])
    for _ in range(10_000):
        auth.authorize(peer.public_hex)
    assert calls["n"] == 1, "10k authorizations inside the TTL = ONE resolve"

    entries.append(fleet_roster.kick(root, machine_pub=peer.public_hex, seq=1))
    auth.authorize(peer.public_hex)  # still inside the window: cached
    clock["t"] += _clock.AUTHORIZE_CACHE_TTL_S + 0.01
    with pytest.raises(HandshakeError):
        auth.authorize(peer.public_hex)
    assert calls["n"] == 2
