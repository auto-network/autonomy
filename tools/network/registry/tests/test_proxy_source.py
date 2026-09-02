"""auto-p20eb: the serve edge carries the real client address in a PROXY v2
header; the ingress consumes it only from a trusted peer.

The forward's emitter (``serve_forward._proxy_v2_header``) and the ingress
parser (``stream_ingress._read_proxy_v2`` plus the trust gate in
``handle_stream_connection``) are exercised together with an in-memory
duplex stream — no sockets, no real TLS.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

from tools.network.registry import stream_ingress as si

# serve_forward.py lives under estate/serve as a standalone script, not an
# importable package path; load it by file.
_SF_PATH = (
    Path(__file__).resolve().parents[2]
    / "estate" / "serve" / "serve_forward.py"
)
_spec = importlib.util.spec_from_file_location("serve_forward", _SF_PATH)
serve_forward = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(serve_forward)


class _MockReader:
    """Feeds a fixed byte string; readexactly/read behave like asyncio's."""

    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0

    async def readexactly(self, n: int) -> bytes:
        if self._pos + n > len(self._data):
            got = self._data[self._pos:]
            self._pos = len(self._data)
            raise asyncio.IncompleteReadError(got, n)
        chunk = self._data[self._pos:self._pos + n]
        self._pos += n
        return chunk

    async def read(self, n: int = -1) -> bytes:
        if self._pos >= len(self._data):
            return b""
        chunk = self._data[self._pos:self._pos + n]
        self._pos += n
        return chunk


class _MockWriter:
    def __init__(self, peer):
        self._peer = peer
        self.buffer = bytearray()
        self.closed = False

    def get_extra_info(self, key):
        return self._peer if key == "peername" else None

    def write(self, data):
        self.buffer.extend(data)

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


# -- emitter (serve_forward) -----------------------------------------------


def test_emitter_ipv4_roundtrips_through_parser():
    header = serve_forward._proxy_v2_header(("203.0.113.7", 51234))

    async def scenario():
        reader = _MockReader(header)
        first16 = await reader.readexactly(16)
        assert first16[:12] == si.PROXY_V2_SIGNATURE
        return await si._read_proxy_v2(first16, reader)

    assert asyncio.run(scenario()) == "203.0.113.7"


def test_emitter_ipv6_roundtrips_through_parser():
    header = serve_forward._proxy_v2_header(("2001:db8::42", 4443))

    async def scenario():
        reader = _MockReader(header)
        first16 = await reader.readexactly(16)
        return await si._read_proxy_v2(first16, reader)

    assert asyncio.run(scenario()) == "2001:db8::42"


def test_emitter_unusable_peer_is_local_command():
    # An address the emitter cannot parse becomes a LOCAL header: the
    # ingress reads it and falls back to the socket peer (returns None).
    header = serve_forward._proxy_v2_header(None)

    async def scenario():
        reader = _MockReader(header)
        first16 = await reader.readexactly(16)
        return await si._read_proxy_v2(first16, reader)

    assert asyncio.run(scenario()) is None


# -- parser rejects malformation -------------------------------------------


def test_parser_rejects_oversized_length():
    bad = si.PROXY_V2_SIGNATURE + bytes([0x21, 0x11]) + (9999).to_bytes(2, "big")

    async def scenario():
        return await si._read_proxy_v2(bad, _MockReader(b""))

    with pytest.raises(si.StreamProtocolError):
        asyncio.run(scenario())


def test_parser_rejects_wrong_version():
    bad = si.PROXY_V2_SIGNATURE + bytes([0x31, 0x11]) + (12).to_bytes(2, "big")

    async def scenario():
        return await si._read_proxy_v2(bad, _MockReader(b"\x00" * 12))

    with pytest.raises(si.StreamProtocolError):
        asyncio.run(scenario())


# -- trust gate in handle_stream_connection --------------------------------


def _fake_client_hello(host: str) -> bytes:
    from tools.network.relaykit.tests.test_stream_wire import _client_hello
    return _client_hello(host)


class _NoRouteHostRoutes:
    """Routes nothing — every stream ends at the route() miss, so the run
    stops right after the source is resolved. We assert on the source the
    limiter was asked to admit."""

    def route(self, sni):
        return None

    def reservation_for(self, sni):
        return ""


class _RecordingLimiter:
    def __init__(self):
        self.begun = []

    def begin(self, source):
        self.begun.append(source)
        return None  # deny → returns immediately after recording the source


def _run_ingress(peer, prefix: bytes, hello_host: str, proxy_sources):
    reader = _MockReader(prefix + _fake_client_hello(hello_host))
    writer = _MockWriter(peer)
    limiter = _RecordingLimiter()
    asyncio.run(si.handle_stream_connection(
        reader, writer,
        host_routes=_NoRouteHostRoutes(), abuse_limiter=limiter,
        proxy_sources=proxy_sources,
    ))
    return limiter.begun


def test_trusted_peer_header_sets_real_source():
    header = serve_forward._proxy_v2_header(("198.51.100.9", 33333))
    begun = _run_ingress(
        ("127.0.0.1", 40000), header, "app.p.serve.auto.network",
        frozenset({"127.0.0.1"}),
    )
    assert begun == ["198.51.100.9"]


def test_untrusted_peer_header_is_dropped():
    # A header from a peer NOT in proxy_sources is never parsed as PROXY;
    # its signature bytes become the "ClientHello", which has no SNI, so
    # the connection closes having admitted nobody.
    header = serve_forward._proxy_v2_header(("198.51.100.9", 33333))
    begun = _run_ingress(
        ("203.0.113.200", 40000), header, "app.p.serve.auto.network",
        frozenset({"127.0.0.1"}),
    )
    assert begun == []  # dropped before admission


def test_trusted_peer_without_header_is_still_tls():
    # Rollout safety: a forward that predates PROXY v2 sends no header. The
    # trusted peer's first bytes are a real ClientHello, parsed as TLS, and
    # the source falls back to the socket peer.
    begun = _run_ingress(
        ("127.0.0.1", 40000), b"", "app.p.serve.auto.network",
        frozenset({"127.0.0.1"}),
    )
    assert begun == ["127.0.0.1"]
