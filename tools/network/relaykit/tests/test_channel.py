"""E2E channel crypto — handshake pinning (I5) and the record layer."""

from __future__ import annotations

import asyncio
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from tools.network.idkit import KeyPair, Subject, canonical_json, issue_cert
from tools.network.relaykit.channel import (
    CHUNK_SIZE,
    MAX_RECORD_CHUNK_SIZE,
    ChannelCrypto,
    HandshakeError,
    RecordError,
    build_client_hello,
    build_server_hello,
    parse_client_hello,
    verify_server_hello,
)
from tools.network.relaykit.frames import (
    VIEWER_KIND_FEED,
    VIEWER_KIND_LEN,
    VIEWER_KIND_RECORD,
    split_viewer_message,
    tag_viewer_message,
)
from tools.network.relaykit.connector import serve_channel
from tools.network.relaykit.viewer import ViewerChannel
from tools.network.relaykit.ice_signaling import (
    IceAnswer,
    IceCapacity,
    IceConfiguration,
    IceSignalingSession,
    STUN_URL,
    TURN_URLS,
)

from .conftest import ORG, TOKEN


def handshake(root, session_key, session_cert, now):
    """Run a full happy-path handshake; returns (client_crypto, server_crypto)."""
    client_priv, client_hello = build_client_hello()
    client_eph = parse_client_hello(client_hello)
    server_priv, server_hello, server_th = build_server_hello(
        session_key, session_cert, org=ORG, token=TOKEN, client_eph=client_eph
    )
    server_eph, client_th = verify_server_hello(
        server_hello, root_pub=root.public_hex, org=ORG, token=TOKEN,
        client_eph=client_eph, now=now,
    )
    assert client_th == server_th
    return (
        ChannelCrypto.client(client_priv, server_eph, client_th),
        ChannelCrypto.server(server_priv, client_eph, server_th),
    )


class TestHandshake:
    def test_happy_path_both_directions(self, root, session_key, session_cert, now):
        client, server = handshake(root, session_key, session_cert, now)
        for record in client.seal_message(b"hello from the viewer"):
            message = server.open_record(record)
        assert message == b"hello from the viewer"
        for record in server.seal_message(b"hello from the dashboard"):
            message = client.open_record(record)
        assert message == b"hello from the dashboard"

    def _server_hello(self, session_key, session_cert, client_eph):
        _, server_hello, _ = build_server_hello(
            session_key, session_cert, org=ORG, token=TOKEN, client_eph=client_eph
        )
        return server_hello

    def test_mitm_server_eph_substitution_fails(self, root, session_key, session_cert, now):
        """I5 core: a relay swapping in its OWN ECDH key cannot fix the
        signature, so the viewer refuses the handshake."""
        _, client_hello = build_client_hello()
        client_eph = parse_client_hello(client_hello)
        server_hello = self._server_hello(session_key, session_cert, client_eph)

        mitm_eph = X25519PrivateKey.generate().public_key().public_bytes_raw().hex()
        tampered = json.loads(server_hello)
        tampered["eph_pub"] = mitm_eph
        with pytest.raises(HandshakeError):
            verify_server_hello(
                canonical_json(tampered), root_pub=root.public_hex, org=ORG,
                token=TOKEN, client_eph=client_eph, now=now,
            )

    def test_mitm_client_eph_substitution_fails(self, root, session_key, session_cert, now):
        """The other MITM half: if the relay swapped the CLIENT eph on the
        way to the dashboard, the dashboard signs the wrong client_eph and
        the real viewer detects it."""
        _, client_hello = build_client_hello()
        real_client_eph = parse_client_hello(client_hello)
        swapped_eph = X25519PrivateKey.generate().public_key().public_bytes_raw().hex()
        # Dashboard saw (and signed) the relay's key, not the viewer's.
        server_hello = self._server_hello(session_key, session_cert, swapped_eph)
        with pytest.raises(HandshakeError):
            verify_server_hello(
                server_hello, root_pub=root.public_hex, org=ORG, token=TOKEN,
                client_eph=real_client_eph, now=now,
            )

    def test_mitm_cert_substitution_fails(self, root, now):
        """A relay minting its own root+cert chain can sign anything — but
        it cannot chain to the org root the viewer pinned from the envelope."""
        fake_root, fake_session = KeyPair.generate(), KeyPair.generate()
        fake_cert = issue_cert(
            fake_root, fake_session.public_hex, scope=("tunnel:serve",), org=ORG,
            subject=Subject("operator", "mallory"),
            not_before=now - 300, not_after=now + 86_400,
        )
        _, client_hello = build_client_hello()
        client_eph = parse_client_hello(client_hello)
        _, server_hello, _ = build_server_hello(
            fake_session, fake_cert, org=ORG, token=TOKEN, client_eph=client_eph
        )
        with pytest.raises(HandshakeError):
            verify_server_hello(
                server_hello, root_pub=root.public_hex, org=ORG, token=TOKEN,
                client_eph=client_eph, now=now,
            )

    def test_cert_without_tunnel_serve_scope_fails(self, root, session_key, now):
        publisher = KeyPair.generate()
        publisher_cert = issue_cert(
            root, publisher.public_hex, scope=("link:publish",), org=ORG,
            subject=Subject("operator", "op-1"),
            not_before=now - 300, not_after=now + 86_400,
        )
        _, client_hello = build_client_hello()
        client_eph = parse_client_hello(client_hello)
        _, server_hello, _ = build_server_hello(
            publisher, publisher_cert, org=ORG, token=TOKEN, client_eph=client_eph
        )
        with pytest.raises(HandshakeError):
            verify_server_hello(
                server_hello, root_pub=root.public_hex, org=ORG, token=TOKEN,
                client_eph=client_eph, now=now,
            )

    def test_token_binding(self, root, session_key, session_cert, now):
        """A SERVER_HELLO minted for one token cannot be replayed onto a
        channel for another (the sig covers the token)."""
        _, client_hello = build_client_hello()
        client_eph = parse_client_hello(client_hello)
        server_hello = self._server_hello(session_key, session_cert, client_eph)
        with pytest.raises(HandshakeError):
            verify_server_hello(
                server_hello, root_pub=root.public_hex, org=ORG,
                token="f" * 32, client_eph=client_eph, now=now,
            )

    @pytest.mark.asyncio
    async def test_viewer_authenticates_an_already_open_transport(
        self, root, session_key, session_cert, now
    ):
        class Transport:
            def __init__(self):
                self.sent = []
                self.replies = asyncio.Queue()
                self.closed = False
                self.server_crypto = None

            async def send(self, payload):
                self.sent.append(payload)
                if self.server_crypto is None:
                    client_eph = parse_client_hello(payload)
                    server_priv, server_hello, transcript = build_server_hello(
                        session_key,
                        session_cert,
                        org=ORG,
                        token=TOKEN,
                        client_eph=client_eph,
                    )
                    self.server_crypto = ChannelCrypto.server(
                        server_priv, client_eph, transcript
                    )
                    await self.replies.put(
                        tag_viewer_message(VIEWER_KIND_RECORD, server_hello)
                    )
                    return
                assert self.server_crypto.open_record(payload) == b"request"
                for record in self.server_crypto.seal_message(b"response"):
                    await self.replies.put(
                        tag_viewer_message(VIEWER_KIND_RECORD, record)
                    )

            async def recv(self):
                return await self.replies.get()

            async def close(self):
                self.closed = True

        transport = Transport()
        channel = await ViewerChannel.authenticate(
            transport,
            TOKEN,
            root_pub=root.public_hex,
            org=ORG,
            now=now,
        )
        await channel.send_message(b"request")
        assert await channel.recv_message() == b"response"
        await channel.close()
        assert transport.closed is True

    @pytest.mark.asyncio
    async def test_viewer_bounds_demultiplexed_feed_while_waiting_for_a_record(
        self, root, session_key, session_cert, now
    ):
        class Transport:
            closed = False

            def __init__(self):
                self.replies = asyncio.Queue()

            async def send(self, payload):
                client_eph = parse_client_hello(payload)
                _, server_hello, _ = build_server_hello(
                    session_key,
                    session_cert,
                    org=ORG,
                    token=TOKEN,
                    client_eph=client_eph,
                )
                await self.replies.put(
                    tag_viewer_message(VIEWER_KIND_RECORD, server_hello)
                )
                feed = tag_viewer_message(VIEWER_KIND_FEED, b"x" * (2 * 1024 * 1024))
                for _ in range(5):
                    await self.replies.put(feed)

            async def recv(self):
                return await self.replies.get()

            async def close(self):
                self.closed = True

        transport = Transport()
        channel = await ViewerChannel.authenticate(
            transport,
            TOKEN,
            root_pub=root.public_hex,
            org=ORG,
            now=now,
        )
        with pytest.raises(ConnectionError, match="demultiplexer queue is full"):
            await channel.recv_message()
        assert transport.closed is True

    @pytest.mark.asyncio
    async def test_viewer_closes_an_open_transport_when_authentication_fails(
        self, root, now
    ):
        class Transport:
            closed = False

            async def send(self, payload):
                pass

            async def recv(self):
                return tag_viewer_message(VIEWER_KIND_RECORD, b"not-json")

            async def close(self):
                self.closed = True

        transport = Transport()
        with pytest.raises(HandshakeError):
            await ViewerChannel.authenticate(
                transport,
                TOKEN,
                root_pub=root.public_hex,
                org=ORG,
                now=now,
            )
        assert transport.closed is True


class TestRecordLayer:
    def test_new_records_fit_smallest_webrtc_message_limit(
        self, root, session_key, session_cert, now
    ):
        client, _server = handshake(root, session_key, session_cert, now)
        records = client.seal_message(b"x" * (CHUNK_SIZE + 1))
        assert len(records) == 2
        # 8-byte sequence + 1-byte encrypted flags + plaintext + 16-byte tag.
        assert max(map(len, records)) <= 65_536

    def test_receiver_keeps_previous_record_ceiling(
        self, root, session_key, session_cert, now
    ):
        client, server = handshake(root, session_key, session_cert, now)
        legacy_record = client._seal_record(
            0x03, b"x" * MAX_RECORD_CHUNK_SIZE
        )
        assert server.open_record(legacy_record) == b"x" * MAX_RECORD_CHUNK_SIZE

    def test_chunked_soak_1_55mb(self, root, session_key, session_cert, now):
        """Q3 in-memory: a binder-sized message survives chunking intact."""
        client, server = handshake(root, session_key, session_cert, now)
        payload = bytes(range(256)) * (1_550_000 // 256 + 1)  # ≈1.55 MB
        records = client.seal_message(payload)
        assert len(records) == len(payload) // CHUNK_SIZE + 1
        result = None
        for record in records:
            out = server.open_record(record)
            if out is not None:
                assert result is None
                result = out
        assert result == payload

    def test_tampered_record_rejected(self, root, session_key, session_cert, now):
        client, server = handshake(root, session_key, session_cert, now)
        record = bytearray(client.seal_message(b"data")[0])
        record[-1] ^= 0x01
        with pytest.raises(RecordError):
            server.open_record(bytes(record))

    def test_replayed_record_rejected(self, root, session_key, session_cert, now):
        client, server = handshake(root, session_key, session_cert, now)
        record = client.seal_message(b"data")[0]
        assert server.open_record(record) == b"data"
        with pytest.raises(RecordError):
            server.open_record(record)

    def test_reordered_records_rejected(self, root, session_key, session_cert, now):
        client, server = handshake(root, session_key, session_cert, now)
        first = client.seal_message(b"x" * (CHUNK_SIZE + 1))
        with pytest.raises(RecordError):
            server.open_record(first[1])

    def test_direction_separation(self, root, session_key, session_cert, now):
        """A record sealed by the client cannot be opened as if it came
        from the server (distinct directional keys + nonces + AAD)."""
        client, _server = handshake(root, session_key, session_cert, now)
        record = client.seal_message(b"data")[0]
        with pytest.raises(RecordError):
            client.open_record(record)  # client expects s2c records

    def test_cross_channel_separation(self, root, session_key, session_cert, now):
        """Records from one channel cannot be spliced into another — keys
        derive from each handshake's transcript."""
        client_a, _ = handshake(root, session_key, session_cert, now)
        _, server_b = handshake(root, session_key, session_cert, now)
        with pytest.raises(RecordError):
            server_b.open_record(client_a.seal_message(b"data")[0])

    def test_empty_message_roundtrip(self, root, session_key, session_cert, now):
        client, server = handshake(root, session_key, session_cert, now)
        records = client.seal_message(b"")
        assert len(records) == 1
        assert server.open_record(records[0]) == b""

    def test_oversize_message_rejected(self, root, session_key, session_cert, now):
        client, server = handshake(root, session_key, session_cert, now)
        server._max_message_size = 16
        with pytest.raises(RecordError):
            for record in client.seal_message(b"z" * 64):
                server.open_record(record)

    def test_oversize_outgoing_message_rejected_before_sealing(
        self, root, session_key, session_cert, now
    ):
        client, _server = handshake(root, session_key, session_cert, now)
        client._max_message_size = 16
        with pytest.raises(RecordError, match="message exceeds maximum size"):
            client.seal_message(b"z" * 17)
        assert client._send_seq == 0

    def test_streaming_message_boundaries_without_whole_exchange_buffer(
        self, root, session_key, session_cert, now
    ):
        client, server = handshake(root, session_key, session_cert, now)
        messages = [
            b"a" * (CHUNK_SIZE + 17),
            b"",
            b"last",
        ]
        records = []
        for index, message in enumerate(messages):
            records.extend(
                client.seal_message(
                    message, stream_final=index == len(messages) - 1
                )
            )

        delivered = []
        current = bytearray()
        boundaries = []
        for record in records:
            opened = server.open_stream_record(record)
            current.extend(opened.chunk)
            if opened.message_end:
                delivered.append(bytes(current))
                current.clear()
                boundaries.append(opened.stream_final)

        assert delivered == messages
        assert boundaries == [False, False, True]
        assert server.buffered_bytes == 0
        assert server.peak_buffered_bytes == 0

    def test_legacy_final_bit_still_ends_a_message(
        self, root, session_key, session_cert, now
    ):
        """Deployed browser clients send the original sole 0x01 final bit."""
        client, server = handshake(root, session_key, session_cert, now)
        legacy_record = client._seal_record(0x01, b"legacy")
        assert server.open_record(legacy_record) == b"legacy"

    def test_stream_tamper_stops_before_later_plaintext(
        self, root, session_key, session_cert, now
    ):
        client, server = handshake(root, session_key, session_cert, now)
        first_message = client.seal_message(
            b"a" * (CHUNK_SIZE + 5), stream_final=False
        )
        later_message = client.seal_message(b"must not arrive", stream_final=True)
        tampered = bytearray(first_message[1])
        tampered[-1] ^= 0x01
        records = [first_message[0], bytes(tampered), *later_message]

        delivered = bytearray()
        with pytest.raises(RecordError):
            for record in records:
                opened = server.open_stream_record(record)
                delivered.extend(opened.chunk)

        assert delivered == b"a" * CHUNK_SIZE

    def test_stream_reorder_stops_before_delivery(
        self, root, session_key, session_cert, now
    ):
        client, server = handshake(root, session_key, session_cert, now)
        records = client.seal_message(
            b"a" * (CHUNK_SIZE + 1), stream_final=True
        )
        delivered = bytearray()
        with pytest.raises(RecordError):
            for record in reversed(records):
                opened = server.open_stream_record(record)
                delivered.extend(opened.chunk)
        assert delivered == b""


@pytest.mark.asyncio
async def test_serve_channel_streams_async_iterator_with_bounded_lookahead(
    root, session_key, session_cert, now
):
    """The handler exchange is streamed as messages, not one aggregate."""
    to_server: asyncio.Queue = asyncio.Queue()
    from_server: asyncio.Queue = asyncio.Queue()
    produced = 0
    completed = 0
    peak_outstanding = 0
    observer = None
    largest_wire_record = 0
    app_messages = [
        b"a" * (1024 * 1024),
        b"b" * (1024 * 1024),
        b"tail",
    ]

    async def handler(token, request):
        assert token == TOKEN
        assert request == b"request"
        nonlocal produced, peak_outstanding
        for message in app_messages:
            produced += 1
            peak_outstanding = max(peak_outstanding, produced - completed)
            yield message

    async def recv():
        return await to_server.get()

    async def send(payload):
        # serve_channel tags what it sends a viewer; this stands in for the
        # viewer, so it strips the kind exactly as a real client does.
        nonlocal completed, largest_wire_record
        kind, record = split_viewer_message(payload)
        assert kind == VIEWER_KIND_RECORD
        if observer is not None:
            largest_wire_record = max(largest_wire_record, len(payload))
            opened = observer.open_stream_record(record)
            if opened.message_end:
                completed += 1
        await from_server.put(record)

    client_priv, client_hello = build_client_hello()
    client_eph = parse_client_hello(client_hello)
    await to_server.put(client_hello)
    task = asyncio.create_task(
        serve_channel(
            session_key,
            session_cert,
            org=ORG,
            token=TOKEN,
            recv=recv,
            send=send,
            handler=handler,
        )
    )

    server_hello = await from_server.get()
    server_eph, transcript_hash = verify_server_hello(
        server_hello,
        root_pub=root.public_hex,
        org=ORG,
        token=TOKEN,
        client_eph=client_eph,
        now=now,
    )
    client = ChannelCrypto.client(client_priv, server_eph, transcript_hash)
    observer = ChannelCrypto.client(client_priv, server_eph, transcript_hash)
    for record in client.seal_message(b"request"):
        await to_server.put(record)

    delivered = []
    current = bytearray()
    while True:
        opened = client.open_stream_record(await from_server.get())
        current.extend(opened.chunk)
        if opened.message_end:
            delivered.append(bytes(current))
            current.clear()
        if opened.stream_final:
            break

    await to_server.put(None)
    await task

    assert delivered == app_messages
    assert completed == produced == len(app_messages)
    assert peak_outstanding <= 2
    # kind byte + 8-byte seq + flags byte + chunk + GCM tag. The kind byte
    # is the framing cost of distinguishing records from feed frames.
    assert largest_wire_record <= VIEWER_KIND_LEN + 8 + 1 + CHUNK_SIZE + 16
    assert client.peak_buffered_bytes == 0


@pytest.mark.asyncio
async def test_serve_channel_isolates_and_closes_per_connection_handler_state(
    root, session_key, session_cert, now
):
    """A stateful capability is scoped to one handshaken connection.

    It must not use the bearer token as a session key: two browsers can hold
    the same public link concurrently. Teardown also has to run on ordinary
    EOF so a peer connection or capacity slot cannot leak.
    """
    to_server: asyncio.Queue = asyncio.Queue()
    from_server: asyncio.Queue = asyncio.Queue()
    opened = []

    class PerConnection:
        def __init__(self):
            self.requests = 0
            self.closed = False

        async def __call__(self, token, request):
            assert token == TOKEN
            self.requests += 1
            return f"{self.requests}:".encode() + request

        async def aclose(self):
            self.closed = True

    class Factory:
        def for_channel(self, token):
            assert token == TOKEN
            state = PerConnection()
            opened.append(state)
            return state

        async def __call__(self, token, request):  # pragma: no cover
            raise AssertionError("factory itself must not serve channel messages")

    async def recv():
        return await to_server.get()

    async def send(payload):
        kind, record = split_viewer_message(payload)
        assert kind == VIEWER_KIND_RECORD
        await from_server.put(record)

    client_priv, client_hello = build_client_hello()
    client_eph = parse_client_hello(client_hello)
    await to_server.put(client_hello)
    task = asyncio.create_task(serve_channel(
        session_key,
        session_cert,
        org=ORG,
        token=TOKEN,
        recv=recv,
        send=send,
        handler=Factory(),
    ))

    server_hello = await from_server.get()
    server_eph, transcript_hash = verify_server_hello(
        server_hello,
        root_pub=root.public_hex,
        org=ORG,
        token=TOKEN,
        client_eph=client_eph,
        now=now,
    )
    client = ChannelCrypto.client(client_priv, server_eph, transcript_hash)

    for request, expected in ((b"one", b"1:one"), (b"two", b"2:two")):
        for record in client.seal_message(request):
            await to_server.put(record)
        response = None
        while response is None:
            response = client.open_record(await from_server.get())
        assert response == expected

    await to_server.put(None)
    await task
    assert len(opened) == 1
    assert opened[0].closed is True


def _test_ice_configuration():
    return IceConfiguration(
        ice_servers=(
            {"urls": [STUN_URL]},
            {
                "urls": list(TURN_URLS),
                "username": "2000:synthetic-test-user",
                "credential": "synthetic-test-credential",
                "credentialType": "password",
            },
        ),
        expires_at=2000,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_terminal_send", [False, True])
async def test_serve_channel_transfers_ice_only_after_terminal_wire_send(
    root, session_key, session_cert, now, fail_terminal_send
):
    """The transport confirmation is the ownership linearization point."""
    to_server: asyncio.Queue = asyncio.Queue()
    from_server: asyncio.Queue = asyncio.Queue()
    capacity = IceCapacity(2, per_token_limit=1)

    class Adapter:
        def __init__(self):
            self.transferred = False
            self.closed = False

        async def answer(self, offer, *, timeout):
            return IceAnswer(sdp="v=0\r\n", candidates=())

        def transfer(self):
            self.transferred = True

        async def aclose(self):
            self.closed = True

    adapter = Adapter()

    class Factory:
        def for_channel(self, token):
            return IceSignalingSession(
                token=token,
                policy="direct_allowed",
                configuration_provider=lambda _token, _policy: _test_ice_configuration(),
                responder_factory=lambda _config, _policy: adapter,
                capacity=capacity,
                wall_clock=lambda: 1000,
            )

    async def recv():
        return await to_server.get()

    sends = 0

    async def send(payload):
        nonlocal sends
        sends += 1
        kind, record = split_viewer_message(payload)
        assert kind == VIEWER_KIND_RECORD
        # SERVER_HELLO, config record, then terminal answer record.
        if fail_terminal_send and sends == 3:
            raise OSError("synthetic terminal transport failure")
        await from_server.put(record)

    client_priv, client_hello = build_client_hello()
    client_eph = parse_client_hello(client_hello)
    await to_server.put(client_hello)
    task = asyncio.create_task(serve_channel(
        session_key, session_cert, org=ORG, token=TOKEN,
        recv=recv, send=send, handler=Factory(),
    ))
    server_hello = await from_server.get()
    server_eph, transcript_hash = verify_server_hello(
        server_hello, root_pub=root.public_hex, org=ORG, token=TOKEN,
        client_eph=client_eph, now=now,
    )
    client = ChannelCrypto.client(client_priv, server_eph, transcript_hash)

    async def exchange(value):
        for record in client.seal_message(canonical_json(value)):
            await to_server.put(record)
        response = None
        while response is None:
            response = client.open_record(await from_server.get())
        return json.loads(response)

    config = await exchange({"v": 1, "op": "ice.begin", "attempt_id": "ab" * 16})
    assert config["op"] == "ice.config"
    offer = {
        "v": 1, "op": "ice.offer", "attempt_id": "ab" * 16,
        "sdp": "v=0\r\n", "candidates": [],
    }
    if fail_terminal_send:
        for record in client.seal_message(canonical_json(offer)):
            await to_server.put(record)
        with pytest.raises(OSError, match="terminal transport failure"):
            await task
        assert adapter.transferred is False
        assert adapter.closed is True
    else:
        answer = await exchange(offer)
        assert answer["op"] == "ice.answer"
        # Browser closes signaling after receiving the terminal answer.
        await to_server.put(None)
        await task
        assert adapter.transferred is True
        assert adapter.closed is False
    assert capacity.active == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("send_begin", [False, True])
async def test_serve_channel_deadline_closes_silent_signaling_peer(
    root, session_key, session_cert, now, send_begin
):
    """Silence before begin or after config cannot retain a coroutine/cap."""
    to_server: asyncio.Queue = asyncio.Queue()
    from_server: asyncio.Queue = asyncio.Queue()
    capacity = IceCapacity(2, per_token_limit=1)

    class Factory:
        def for_channel(self, token):
            return IceSignalingSession(
                token=token,
                policy="direct_allowed",
                configuration_provider=lambda _token, _policy: _test_ice_configuration(),
                responder_factory=lambda _config, _policy: None,
                capacity=capacity,
                wall_clock=lambda: 1000,
                deadline_seconds=0.2,
            )

    async def recv():
        return await to_server.get()

    async def send(payload):
        kind, record = split_viewer_message(payload)
        assert kind == VIEWER_KIND_RECORD
        await from_server.put(record)

    client_priv, client_hello = build_client_hello()
    client_eph = parse_client_hello(client_hello)
    await to_server.put(client_hello)
    task = asyncio.create_task(serve_channel(
        session_key, session_cert, org=ORG, token=TOKEN,
        recv=recv, send=send, handler=Factory(),
    ))
    server_hello = await from_server.get()
    server_eph, transcript_hash = verify_server_hello(
        server_hello, root_pub=root.public_hex, org=ORG, token=TOKEN,
        client_eph=client_eph, now=now,
    )
    client = ChannelCrypto.client(client_priv, server_eph, transcript_hash)
    if send_begin:
        for record in client.seal_message(canonical_json({
            "v": 1, "op": "ice.begin", "attempt_id": "ab" * 16,
        })):
            await to_server.put(record)
        response = None
        while response is None:
            response = client.open_record(await from_server.get())
        assert json.loads(response)["op"] == "ice.config"
        assert capacity.active == 1
    with pytest.raises(asyncio.TimeoutError):
        await task
    assert capacity.active == 0
