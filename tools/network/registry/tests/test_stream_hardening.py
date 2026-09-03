"""auto-ohlx3: adversarial hardening of the raw-TLS ingress path.

The code-testable half of the bead: relay plaintext opacity, connection-flood
stream-cap refusal, and malformed-ClientHello zero-byte refusal, at the
ingress boundary. The restart/recovery matrix (Dashboard/connector/Caddy/reboot
recovery) is the joint live drill auto-hrq0v, not a unit test.
"""

from __future__ import annotations

import asyncio
import logging

from tools.network.relaykit.stream_wire import (
    CAP_TLS_STREAM, STREAM_MAX_PER_TUNNEL,
)
from tools.network.registry import stream_ingress as si
from tools.network.registry.metrics import RegistryMetrics


class _MockReader:
    def __init__(self, data: bytes):
        self._data, self._pos = data, 0

    async def readexactly(self, n):
        if self._pos + n > len(self._data):
            got = self._data[self._pos:]
            self._pos = len(self._data)
            raise asyncio.IncompleteReadError(got, n)
        chunk = self._data[self._pos:self._pos + n]
        self._pos += n
        return chunk

    async def read(self, n=-1):
        if self._pos >= len(self._data):
            return b""
        chunk = self._data[self._pos:self._pos + n]
        self._pos += n
        return chunk


class _MockWriter:
    def __init__(self, peer=("203.0.113.9", 5000)):
        self._peer = peer
        self.sent_back = bytearray()
        self.closed = False

    def get_extra_info(self, key):
        return self._peer if key == "peername" else None

    def write(self, data):
        self.sent_back.extend(data)

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


def _client_hello(host):
    from tools.network.relaykit.tests.test_stream_wire import _client_hello
    return _client_hello(host)


class _FullTunnel:
    """A capable tunnel already at the per-tunnel stream cap."""

    def __init__(self, org="aaaaaaaa-0000-0000-0000-000000000001"):
        self.org = org
        self.caps = {CAP_TLS_STREAM}
        self.raw_streams = {i: object() for i in range(STREAM_MAX_PER_TUNNEL)}


class _CapRoutes:
    def __init__(self, tunnel):
        self._t = tunnel

    def route(self, sni):
        return self._t

    def reservation_for(self, sni):
        return "res"


class _NoRoute:
    def route(self, sni):
        return None

    def reservation_for(self, sni):
        return ""


def test_stream_cap_flood_refused_and_counted():
    """A connection that arrives when the tunnel is already at
    STREAM_MAX_PER_TUNNEL is refused, counted, and gets zero bytes back."""
    metrics = RegistryMetrics()
    writer = _MockWriter()
    reader = _MockReader(_client_hello("app.p.serve.auto.network"))
    asyncio.run(si.handle_stream_connection(
        reader, writer, host_routes=_CapRoutes(_FullTunnel()),
        abuse_limiter=None, metrics=metrics,
    ))
    assert bytes(writer.sent_back) == b""      # byte-identical refusal
    out = metrics.render()
    assert 'relay_stream_refusals_total{reason="stream_cap"} 1' in out


def test_malformed_client_hello_refused_with_zero_bytes():
    metrics = RegistryMetrics()
    writer = _MockWriter()
    reader = _MockReader(b"not a tls client hello at all, just junk bytes")
    asyncio.run(si.handle_stream_connection(
        reader, writer, host_routes=_NoRoute(),
        abuse_limiter=None, metrics=metrics,
    ))
    assert bytes(writer.sent_back) == b""
    out = metrics.render()
    # A non-ClientHello either parses to no-SNI or exhausts the peek bound;
    # both are refusals that write nothing back.
    assert "relay_stream_refusals_total" in out


def test_unrouted_host_refused_and_counted():
    metrics = RegistryMetrics()
    writer = _MockWriter()
    reader = _MockReader(_client_hello("nobody.p.serve.auto.network"))
    asyncio.run(si.handle_stream_connection(
        reader, writer, host_routes=_NoRoute(),
        abuse_limiter=None, metrics=metrics,
    ))
    assert bytes(writer.sent_back) == b""
    assert 'relay_stream_refusals_total{reason="unrouted"} 1' in out_of(metrics)


def out_of(metrics):
    return metrics.render()


def test_ingress_never_logs_payload_or_source_with_token(caplog):
    """Opacity: a refusal path must never emit payload bytes or pair a
    source address with a hostname/token in logs. Drive a connection whose
    SNI and buffered bytes carry a unique sentinel and assert the sentinel
    never reaches the log stream."""
    sentinel = "SENTINEL-9f3c-secret-path-and-cookie"
    metrics = RegistryMetrics()
    writer = _MockWriter()
    # The sentinel rides in a (malformed) hello body — the relay must not log
    # the buffered bytes.
    reader = _MockReader(sentinel.encode() + b"\x00" * 32)
    with caplog.at_level(logging.DEBUG, logger="registry.stream"):
        asyncio.run(si.handle_stream_connection(
            reader, writer, host_routes=_NoRoute(),
            abuse_limiter=None, metrics=metrics,
        ))
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert sentinel not in joined
    # And the metrics exposition never carries payload/source either.
    assert sentinel not in metrics.render()


class _RecordingLimiter:
    """Captures the source string admission is attempted with, then denies
    (so the connection short-circuits after we've observed the source)."""

    def __init__(self):
        self.seen: list[str] = []

    def begin(self, source):
        self.seen.append(source)
        return None  # deny — we only care that `source` is the native peer


def test_source_is_native_socket_peer_no_proxy_header():
    """The ingress binds the public serve IP directly, so admission keys on
    the real socket peer address — there is no PROXY header to parse and no
    trusted forward hop. A ClientHello with a benign SNI must present the
    connecting peer's IP (not a loopback forward address) to the limiter."""
    limiter = _RecordingLimiter()
    writer = _MockWriter(peer=("198.51.100.7", 44321))
    reader = _MockReader(_client_hello("app.p.serve.auto.network"))
    asyncio.run(si.handle_stream_connection(
        reader, writer, host_routes=_NoRoute(),
        abuse_limiter=limiter, metrics=RegistryMetrics(),
    ))
    assert limiter.seen == ["198.51.100.7"]


def test_ingress_startup_line_survives_warning_level_service():
    """The ingress binds the public serve edge; its startup confirmation must
    reach production logs. Routed through the ops sink (its own handler,
    propagate=False), it emits even with the root logger at WARNING — the same
    pitfall class as the DNS-01 audit records (auto-dn6bo)."""
    ops = logging.getLogger("autonomy.registry.ops")
    assert ops.propagate is False and ops.handlers

    records: list = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    capture = _Capture(level=logging.INFO)
    ops.addHandler(capture)
    root = logging.getLogger()
    old = root.level
    root.setLevel(logging.WARNING)

    async def _run():
        server = await si.start_stream_ingress(
            "127.0.0.1", 0, host_routes=_NoRoute())
        server.close()
        await server.wait_closed()

    try:
        asyncio.run(_run())
    finally:
        ops.removeHandler(capture)
        root.setLevel(old)

    assert any("stream.ingress.listening" in m and "port=" in m
               for m in records)
