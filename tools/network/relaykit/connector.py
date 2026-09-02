"""Dashboard-side tunnel connector — spec §5.1: the dashboard dials OUT.

Maintains one outbound WebSocket to the registry relay (``/t/{org}``),
authenticates with a ``tunnel:serve`` hello, then serves E2E channels
muxed down it. Zero inbound ports on the dashboard.

Reconnect: exponential backoff with jitter (``min_backoff`` doubling to
``max_backoff``), reset only after an authenticated tunnel has served for at
least one ``max_backoff`` interval. A successful hello alone is not health:
an immediately failing serve loop must keep backing off. A rejected hello is
retried rather than treated as fatal — certs renew and bindings heal without
operator involvement. With the defaults, health therefore means 5 seconds of
continuous service and a capped retry can sleep for up to 6.25 seconds after
jitter; :meth:`TunnelConnector.stop` takes effect after that current sleep.

Each viewer channel runs its own task: OPEN spawns it, DATA frames feed
its queue, and the E2E handshake + record layer (``channel.py``) happen
entirely inside it — one slow channel never stalls the tunnel read loop
or its siblings.

The *handler* is the application seam (C4 wires the real target
resolver into it): ``async def handler(token, message) -> response``.
A response is either one bytes-like message or an async iterator of
bytes-like messages.  Iterator responses are sent message-by-message,
without materializing the whole exchange. ``EchoHandler`` is the reference
one-shot implementation used by the soak tests.

Runnable directly for tests / manual bring-up::

    python -m tools.network.relaykit.connector \
        --relay ws://127.0.0.1:8477 --org <uuid> \
        --key-file session.hex --cert-file session.cert --mode echo
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import inspect
import json
import logging
import random
import re
import secrets
import time

import websockets

from tools.network.idkit import DelegationCert, KeyPair

from .channel import ChannelCrypto, build_server_hello, parse_client_hello
from .frames import (
    CHANNEL_ID_LEN,
    CTRL_CHANNEL_ID,
    FRAME_CLOSE,
    FRAME_CTRL,
    FRAME_DATA,
    FRAME_STREAM_CTRL,
    VIEWER_KIND_RECORD,
    FRAME_OPEN,
    FrameError,
    decode_frame,
    tag_viewer_message,
    encode_frame,
)
from .stream_adapter import StreamAdapter
from .stream_wire import CAP_TLS_STREAM
from .hello import (
    HELLO_VERSION,
    HELLO_VERSION_2,
    build_tunnel_hello,
    build_tunnel_hello_v2,
)

logger = logging.getLogger(__name__)

_LIFECYCLE_LOG_WINDOW_S = 60.0
_LOG_SECRET_RE = re.compile(
    r"(?:https?|wss?)://\S+|"
    r"\b(?:\d{1,3}\.){3}\d{1,3}\b|"
    r"(?<![0-9a-fA-F:])(?:[0-9a-fA-F]{0,4}:){2,}"
    r"[0-9a-fA-F]{0,4}(?:%[A-Za-z0-9_.-]+)?(?![0-9a-fA-F:])|"
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}\b|"
    r"\b[0-9a-fA-F]{16,}\b|"
    r"\b[A-Za-z0-9_-]{24,}\b"
)
# Control operation vocabulary includes the namespaced DNS-01 operations
# (serve.dns01.present / cleanup). Keep logs bounded, but do not relabel valid
# dotted operations as <invalid> and send diagnosis down the wrong path.
_OP_RE = re.compile(r"^[a-z][a-z0-9.-]{0,63}$")


def _safe_log_text(value) -> str:
    if value is None:
        return ""
    return _LOG_SECRET_RE.sub("<redacted>", str(value))[:160]


def _safe_operation(value) -> str:
    return value if isinstance(value, str) and _OP_RE.fullmatch(value) else "<invalid>"


class TunnelProtocolVersionError(ConnectionError):
    """The connector and registry implement different strict wire versions."""

    def __init__(self, remote_version):
        self.local_version = HELLO_VERSION
        self.remote_version = remote_version
        remote = "missing" if remote_version is None else repr(remote_version)
        super().__init__(
            "tunnel protocol version mismatch: "
            f"connector={self.local_version} registry={remote}"
        )


async def echo_handler(token: str, message: bytes) -> bytes:
    """Reference handler: byte-exact echo (what the soak test asserts)."""
    return message


def _message_bytes(value) -> bytes:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise TypeError("channel handler messages must be bytes-like")
    return bytes(value)


async def _response_messages(response):
    """Yield ``(message, stream_final)`` with one-message lookahead.

    The record format marks the last *real* message in an exchange, so an
    iterator needs one bounded item of lookahead rather than an artificial
    empty terminator. Peak send-side plaintext retained here is therefore
    approximately twice the largest yielded message; streaming handlers MUST
    yield bounded messages (attachment v1 yields one 1 MiB application chunk).
    Closing the iterator in ``finally`` releases an open file/generator when
    transport send is cancelled or fails.
    """
    if isinstance(response, (bytes, bytearray, memoryview)):
        yield _message_bytes(response), True
        return
    if not hasattr(response, "__aiter__"):
        raise TypeError(
            "channel handler response must be bytes-like or an async iterator"
        )

    iterator = aiter(response)
    try:
        try:
            current = _message_bytes(await anext(iterator))
        except StopAsyncIteration as exc:
            raise ValueError("channel handler stream must yield at least one message") from exc
        while True:
            try:
                following = _message_bytes(await anext(iterator))
            except StopAsyncIteration:
                yield current, True
                return
            yield current, False
            current = following
    finally:
        close = getattr(iterator, "aclose", None)
        if close is not None:
            await close()


async def serve_established_channel(
    crypto: ChannelCrypto, *, token: str, recv, send, handler
) -> None:
    """Serve request/response exchanges on an authenticated channel.

    ``send`` is the raw transport sender; pairwise record tagging remains here
    so org-delegated and fleet-authenticated handshakes share one record loop.
    The caller has already authenticated its peer and derived ``crypto``.
    """
    _untagged_send = send

    async def send(payload: bytes) -> None:
        await _untagged_send(tag_viewer_message(VIEWER_KIND_RECORD, payload))

    await _serve_channel_records(
        crypto, token=token, recv=recv, send=send, handler=handler
    )


async def _serve_channel_records(
    crypto: ChannelCrypto, *, token: str, recv, send, handler
) -> None:
    """Serve one established E2E channel; ``send`` is already tagged.

    *recv* returns the next incoming channel record (``None`` ends the
    channel), *send* transmits one outgoing pairwise record. A bytes-like
    handler response is one message; an async iterator response is streamed
    as multiple bounded messages.

    Streaming consumers are responsible for the D1 memory bound: yield
    chunk/window-sized messages, never the whole file. The channel rejects any
    individual message above its symmetric ``MAX_MESSAGE_SIZE`` backstop, and
    the one-item final-boundary lookahead retains at most two yielded messages.
    """
    # Most application handlers are stateless callables and remain byte-for-
    # byte compatible. Stateful channel capabilities (ICE signaling is the
    # first) expose ``for_channel(token)`` so each independently handshaken
    # viewer connection gets isolated, teardown-aware state. Token alone is
    # deliberately not used as the state key: several people may hold one
    # public link and open concurrent signaling channels.
    channel_handler = handler
    factory = getattr(handler, "for_channel", None)
    if factory is not None:
        channel_handler = factory(token)
        if inspect.isawaitable(channel_handler):
            channel_handler = await channel_handler

    try:
        while True:
            receive_timeout = getattr(channel_handler, "receive_timeout", None)
            if receive_timeout is None:
                record = await recv()
            else:
                timeout = receive_timeout()
                if inspect.isawaitable(timeout):
                    raise TypeError("channel receive timeout must be synchronous")
                record = (
                    await recv()
                    if timeout is None
                    else await asyncio.wait_for(recv(), timeout=timeout)
                )
            if record is None:
                return
            message = crypto.open_record(record)
            if message is None:
                continue
            response = channel_handler(token, message)
            if inspect.isawaitable(response):
                response = await response
            if response is None:
                continue
            response_messages = _response_messages(response)
            try:
                async for response_message, stream_final in response_messages:
                    for out in crypto.iter_seal_message(
                        response_message, stream_final=stream_final
                    ):
                        await send(out)
            finally:
                await response_messages.aclose()
            # A stateful capability can transfer ownership only after every
            # encrypted record in its response has actually reached the
            # transport.  Keep this hook synchronous: after the final send
            # await returns, no cancellation can interleave with an
            # event-loop-local ownership change.  A true result closes the
            # short-lived capability channel immediately.
            sent = getattr(channel_handler, "on_response_sent", None)
            if sent is not None:
                close_after_response = sent()
                if inspect.isawaitable(close_after_response):
                    raise TypeError("channel response confirmation must be synchronous")
                if close_after_response:
                    return
    finally:
        close = getattr(channel_handler, "aclose", None)
        if close is not None:
            result = close()
            if inspect.isawaitable(result):
                await result


async def serve_channel(key: KeyPair, cert: DelegationCert, *, org: str, token: str,
                        recv, send, handler) -> None:
    """Authenticate an org-delegated peer, then serve its record exchanges."""
    first = await recv()
    if first is None:
        return
    client_eph = parse_client_hello(first)
    eph_priv, server_hello, transcript_hash = build_server_hello(
        key, cert, org=org, token=token, client_eph=client_eph
    )
    await send(tag_viewer_message(VIEWER_KIND_RECORD, server_hello))
    crypto = ChannelCrypto.server(eph_priv, client_eph, transcript_hash)
    await _serve_channel_records(
        crypto,
        token=token,
        recv=recv,
        send=lambda payload: send(tag_viewer_message(VIEWER_KIND_RECORD, payload)),
        handler=handler,
    )


def file_handler(path: str, content_type: str):
    """Serve one HTML file as a design artifact for manual channel tests.

    This development seam implements the same part-addressed fetch protocol
    as the grant-gated production resolver. ``content_type`` remains in the
    CLI signature for compatibility and must identify HTML.
    """
    from tools.network.idkit import canonical_json

    if content_type.split(";", 1)[0].strip().lower() != "text/html":
        raise ValueError("serve-file mode requires text/html")
    body = open(path, "rb").read()
    ok = canonical_json({
        "v": 1, "status": "ok", "kind": "design",
        "viewer": {"offset": 0, "length": len(body)},
    }) + b"\n" + body
    head = canonical_json({
        "v": 1, "status": "ok", "serialized_size": len(body),
    }) + b"\n"
    bad = canonical_json({"v": 1, "status": 400}) + b"\nbad request"

    async def handler(token: str, message: bytes) -> bytes:
        try:
            request = json.loads(message)
        except ValueError:
            return bad
        if (
            not isinstance(request, dict)
            or set(request) != {"v", "op"}
            or request.get("v") != 1
        ):
            return bad
        if request.get("op") == "fetch":
            return ok
        if request.get("op") == "head":
            return head
        return bad

    return handler


class Publisher:
    """The push seam (auto-albp6.8): the one object that knows both which
    tokens currently have listeners AND how to emit on the live tunnel.

    The handler seam (``handler(token, message) -> response``) is invoked
    only by an inbound record and never holds a reference to ``send``, so
    there is no path for the serving side to emit anything unsolicited.
    This closes that gap without changing the request/response loop: the
    connector reports channel attach/detach here, hands over its shared
    tunnel ``send_frame`` while a tunnel is up, and the application side
    (``link_serving``) registers a stream key per token when a viewer
    subscribes.

    A publish is addressed BY TOKEN, not by channel: one sealed frame
    leaves the tunnel per batch regardless of audience size, and the
    relay's own Stream (auto-albp6.7) fans it out. That is the whole
    point -- the connector's outbound cost must not scale with viewers.

    The stream key never reaches this class or the relay. It is held by
    the application side, which seals a frame before handing it here;
    ``Publisher`` moves opaque bytes only.
    """

    def __init__(self):
        #: token -> number of channels currently open against it. The
        #: connector does no work for a token nobody is watching, so an
        #: org holding many quiet published links costs nothing.
        self._attached: dict[str, int] = {}
        #: Live only while a tunnel is up; cleared in the serve loop's
        #: finally so a publish between tunnels fails closed rather than
        #: emitting into a dead socket.
        self._send_frame = None
        # token -> exact responder object -> async raw-viewer send.  Relay
        # viewers are fanned out by the registry after one tunnel frame;
        # direct DataChannels have no registry hop, so each direct listener
        # receives the same already-sealed feed bytes here.  The stream key is
        # still absent: this object moves opaque bytes only.
        self._direct: dict[str, dict[object, object]] = {}

    # -- connector side ------------------------------------------------

    def bind(self, send_frame) -> None:
        self._send_frame = send_frame

    def unbind(self) -> None:
        self._send_frame = None

    def attached(self, token: str) -> None:
        self._attached[token] = self._attached.get(token, 0) + 1

    def detached(self, token: str) -> None:
        remaining = self._attached.get(token, 0) - 1
        if remaining > 0:
            self._attached[token] = remaining
        else:
            self._attached.pop(token, None)

    def attach_direct(self, token: str, owner: object, send) -> None:
        """Attach one exact live DataChannel to *token*'s feed."""
        if not callable(send):
            raise TypeError("direct publisher send must be callable")
        listeners = self._direct.setdefault(token, {})
        if owner in listeners:
            raise RuntimeError("direct publisher listener is already attached")
        listeners[owner] = send

    def detach_direct(self, token: str, owner: object) -> None:
        """Detach only *owner*; delayed teardown cannot remove a successor."""
        listeners = self._direct.get(token)
        if listeners is None:
            return
        listeners.pop(owner, None)
        if not listeners:
            self._direct.pop(token, None)

    # -- application side ----------------------------------------------

    def has_listeners(self, token: str) -> bool:
        """Whether any channel is open against *token* right now. The
        publish side checks this before doing any work at all."""
        return token in self._attached or token in self._direct

    async def publish(self, token: str, sealed: bytes) -> bool:
        """Emit one already-sealed frame for *token*, once. False means
        it was not sent -- no tunnel, or nobody is listening -- and is
        not an error: the live stream is best-effort by design and a
        viewer recovers a gap through history on its own channel."""
        if not self.has_listeners(token):
            return False
        try:
            token_bytes = bytes.fromhex(token)
        except ValueError:
            return False
        if len(token_bytes) != CHANNEL_ID_LEN:
            return False
        sent = False
        send_frame = self._send_frame
        if send_frame is not None and token in self._attached:
            try:
                await send_frame(FRAME_DATA, token_bytes, sealed)
                sent = True
            except Exception:
                pass  # a dropped frame is recoverable through history

        # Snapshot: a send failure may concurrently close and detach the exact
        # responder.  Never iterate the mutable owner map across an await.
        direct = tuple(self._direct.get(token, {}).values())
        if direct:
            from .frames import VIEWER_KIND_FEED, tag_viewer_message
            # The sealed feed frame is opaque here, so it cannot be split
            # without changing its application protocol.  Relay transport can
            # carry larger frames; direct DataChannels cannot.  Skip only the
            # oversized direct delivery and leave relay delivery intact rather
            # than tearing down every direct viewer on one large event.
            if len(sealed) <= 60 * 1024 + 26:
                tagged = tag_viewer_message(VIEWER_KIND_FEED, sealed)
                results = await asyncio.gather(
                    *(send(tagged) for send in direct), return_exceptions=True
                )
                sent = sent or any(not isinstance(result, BaseException) for result in results)
        return sent


class TunnelConnector:
    def __init__(
        self,
        relay_url: str,
        org: str,
        key: KeyPair,
        cert: DelegationCert,
        handler=echo_handler,
        *,
        channel_cert: DelegationCert | None = None,
        min_backoff: float = 0.2,
        max_backoff: float = 5.0,
        publisher: "Publisher | None" = None,
        machine_key: KeyPair | None = None,
        caps: tuple = (),
        stream_handler=None,
    ):
        self._url = f"{relay_url.rstrip('/')}/t/{org}"
        self._org = org
        self._key = key
        self._cert = cert
        self._channel_cert = channel_cert or cert
        if (
            channel_cert is not None
            and self._channel_cert.child_pub != key.public_hex
        ):
            raise ValueError("channel cert does not match the connector key")
        self._handler = handler
        #: Optional push seam (auto-albp6.8). None leaves the connector's
        #: behaviour byte-identical to before it existed.
        self._publisher = publisher
        self._min_backoff = min_backoff
        self._max_backoff = max_backoff
        #: Enrolled machine identity: presence selects the v2 hello
        #: (machine co-signature + capability negotiation, auto-0zdky).
        self._machine_key = machine_key
        self._caps = tuple(sorted({str(cap) for cap in caps}))
        if self._caps and machine_key is None:
            raise ValueError("capabilities require a machine key (hello v2)")
        #: async (host, reservation) -> (reader, writer) | None — the
        #: dashboard's dial-only raw-stream seam (stream_adapter.py).
        self._stream_handler = stream_handler
        #: capability intersection the registry accepted on the live tunnel
        self.accepted_caps: tuple = ()
        #: reservation -> hostname this connector wants leased; re-registered
        #: after every reconnect (leases are connection-scoped by design).
        self._desired_hosts: dict = {}
        self._host_leases: dict = {}
        self._stop = asyncio.Event()
        #: set while a tunnel is authenticated and serving (tests await it)
        self.connected = asyncio.Event()
        #: control-frame reply correlation — id -> Future, resolved in the
        #: serve loop; the send hook is live only while a tunnel is up.
        self._pending: dict = {}
        self._ctrl_send = None
        self._failure_log_window_started = None
        self._failure_log_suppressed = 0

    def stop(self) -> None:
        self._stop.set()

    async def control(self, op: str, args: dict, timeout: float = 10.0) -> dict:
        """Send one D19 control op over the live tunnel and await its
        correlated reply (register D19 §3). Raises ConnectionError when no
        tunnel is up or the reply does not arrive within *timeout*."""
        correlation = secrets.token_hex(16)
        safe_op = _safe_operation(op)
        send = self._ctrl_send
        if send is None:
            logger.warning(
                "tunnel control failed: id=%s op=%s kind=no-live-tunnel",
                correlation, safe_op,
            )
            raise ConnectionError("no live tunnel to carry a control frame")
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        self._pending[correlation] = future
        request = {"id": correlation, "op": op, "args": args}
        try:
            await send(FRAME_CTRL, CTRL_CHANNEL_ID,
                       json.dumps(request).encode("utf-8"))
            reply = await asyncio.wait_for(future, timeout)
            if not (isinstance(reply, dict) and reply.get("ok") is True):
                logger.warning(
                    "tunnel control failed: id=%s op=%s kind=rejected",
                    correlation, safe_op,
                )
            return reply
        except asyncio.TimeoutError as exc:
            logger.warning(
                "tunnel control failed: id=%s op=%s kind=timeout",
                correlation, safe_op,
            )
            raise ConnectionError(
                f"control op {op!r} timed out after {timeout}s") from exc
        except Exception as exc:
            logger.warning(
                "tunnel control failed: id=%s op=%s kind=exception err=%s",
                correlation, safe_op, type(exc).__name__,
            )
            raise
        finally:
            self._pending.pop(correlation, None)

    def _machine_digest(self) -> str:
        """A short machine-key digest for probe echoes: distinguishes
        connectors in a routing proof without disclosing the enrolled key
        to an anonymous prober."""
        if self._machine_key is None:
            return ""
        return hashlib.sha256(
            bytes.fromhex(self._machine_key.public_hex)
        ).hexdigest()[:16]

    async def serve_host(self, reservation: str, host: str) -> dict:
        """Advertise + lease one serving hostname (host-lease/1). The pair
        persists as desired state: leases are connection-scoped by design,
        so every reconnect re-registers them under a fresh generation."""
        self._desired_hosts[reservation] = host
        try:
            return await self._register_host(reservation, host)
        except ConnectionError:
            # Desired state is recorded; the per-connection lease keeper
            # registers it as soon as a tunnel is up.
            return {"ok": False, "error": "no-live-tunnel"}

    async def release_host(self, reservation: str) -> dict:
        self._desired_hosts.pop(reservation, None)
        self._host_leases.pop(reservation, None)
        return await self.control("host-release", {"reservation": reservation})

    async def _register_host(self, reservation: str, host: str) -> dict:
        reply = await self.control(
            "host-register", {"reservation": reservation, "host": host}
        )
        if isinstance(reply, dict) and reply.get("ok") is True:
            self._host_leases[reservation] = dict(reply.get("lease") or {})
        return reply

    async def _maintain_host_leases(self) -> None:
        """Per-connection lease keeper: re-register every desired host on
        this fresh tunnel, then keep them all alive with ONE tunnel-wide
        ``host-renew-all`` at roughly half the lease TTL (auto-ja0rf) —
        renewal is a dead-man's switch for the whole connection, so it
        carries no per-reservation state. A registry that predates the op
        answers unknown-op and this keeper falls back to per-reservation
        renewal for the life of the connection."""
        self._host_leases = {}
        for reservation, host in list(self._desired_hosts.items()):
            with contextlib.suppress(Exception):
                await self._register_host(reservation, host)
        renew_all_supported = True
        # Jitter is fixed per connection so a fleet of connectors spreads
        # its keepalives instead of thundering together after a relay
        # restart: renew when the earliest lease is within margin of
        # expiry (margin 240..300 s of the 600 s TTL → one op ~every
        # 300-360 s).
        renew_margin = 240 + random.random() * 60
        while True:
            await asyncio.sleep(15)
            now = time.time()
            # Registration repair: desired hosts with no lease on this
            # connection (registration raced the connect, or the registry
            # dropped one) are re-registered before renewal is considered.
            for reservation, host in list(self._desired_hosts.items()):
                if reservation not in self._host_leases:
                    with contextlib.suppress(Exception):
                        await self._register_host(reservation, host)
            if not self._host_leases:
                continue
            if renew_all_supported:
                remaining = min(
                    lease.get("expires_at", 0)
                    for lease in self._host_leases.values()
                ) - now
                if remaining > renew_margin:
                    continue
                try:
                    reply = await self.control("host-renew-all", {})
                except Exception:
                    continue  # tunnel churn: next tick (or reconnect) retries
                if reply.get("ok") is True:
                    expires_at = reply.get("expires_at")
                    for reservation in list(self._host_leases):
                        self._host_leases[reservation]["expires_at"] = expires_at
                    if reply.get("renewed", 0) < len(self._host_leases):
                        # The registry renewed fewer leases than we hold:
                        # some expired server-side. Re-registering our own
                        # live reservations is permitted, so repair all.
                        for reservation, host in list(
                            self._desired_hosts.items()
                        ):
                            with contextlib.suppress(Exception):
                                await self._register_host(reservation, host)
                elif "unknown control op" in str(reply.get("error", "")):
                    renew_all_supported = False
                continue
            # Legacy path (pre-auto-ja0rf registry): renew each lease at
            # its own half-life with the generation-fenced per-share op.
            for reservation, lease in list(self._host_leases.items()):
                host = self._desired_hosts.get(reservation)
                if host is None:
                    continue
                if lease.get("expires_at", 0) - now > 60:
                    continue
                try:
                    reply = await self.control("host-renew", {
                        "reservation": reservation,
                        "generation": lease.get("generation"),
                    })
                    if reply.get("ok") is True:
                        self._host_leases[reservation] = dict(reply["lease"])
                    elif reply.get("error") == "stale-generation":
                        await self._register_host(reservation, host)
                except Exception:
                    continue  # tunnel churn: the next tick (or reconnect) retries

    def _resolve_ctrl_reply(self, payload: bytes) -> None:
        """Deliver a FRAME_CTRL reply to its waiting control() caller."""
        try:
            reply = json.loads(payload.decode("utf-8"))
            correlation = reply["id"]
        except (ValueError, KeyError, TypeError):
            return  # unparseable reply: the caller times out honestly
        future = self._pending.get(correlation)
        if future is not None and not future.done():
            future.set_result(reply)

    async def run(self) -> None:
        """Dial, serve, and re-dial until :meth:`stop`."""
        backoff = self._min_backoff
        while not self._stop.is_set():
            # Lifetime of THIS attempt. A tunnel that handshakes and then dies
            # instantly is indistinguishable from a healthy one at the hello,
            # so the duration is the only thing that tells them apart — it is
            # both what gets logged and what a reset-on-healthy backoff needs.
            served_at = None
            disconnect_exc = None
            try:
                # compression=None: mux frames are E2E ciphertext (I5) —
                # incompressible anyway, and literal bytes keep the
                # ciphertext-on-the-wire property directly observable.
                async with websockets.connect(self._url, max_size=2**22,
                                              compression=None) as ws:
                    await self._handshake(ws)
                    served_at = time.monotonic()
                    self.connected.set()
                    # Always run the keeper for a live tunnel. Production adds
                    # publications after the connector is already connected;
                    # gating task creation on desired_hosts-at-handshake left
                    # those late leases registered once but never renewed.
                    lease_task = asyncio.create_task(
                        self._maintain_host_leases()
                    )
                    try:
                        await self._serve(ws)
                    finally:
                        self.connected.clear()
                        lease_task.cancel()
                        with contextlib.suppress(
                            asyncio.CancelledError, Exception
                        ):
                            await lease_task
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # NOT silent: every close the relay sends — 1001 going away,
                # 4409 replaced, a rejected hello — arrives here. Swallowing it
                # made a saturating reconnect storm undiagnosable from the
                # connector side while it was actively happening.
                disconnect_exc = exc
            # One calculation for both a clean serve-loop return and an
            # exceptional disconnect. Measure before logging: a blocked log
            # sink is not useful tunnel service and must not reset health.
            served_for = (None if served_at is None
                          else time.monotonic() - served_at)
            # Authentication proves who answered, not that the connection was
            # useful. Reset only after it stayed up long enough to distinguish
            # ordinary churn from a post-hello flap. Reuse max_backoff as the
            # stability interval: no second timing knob or policy surface.
            if served_for is not None and served_for >= self._max_backoff:
                backoff = self._min_backoff
                self._reset_failure_log_suppression()
            if self._stop.is_set():
                self._log_disconnect(disconnect_exc, served_for, None)
                return
            retry_delay = backoff * (1 + random.random() * 0.25)
            self._log_disconnect(disconnect_exc, served_for, retry_delay)
            await asyncio.sleep(retry_delay)
            backoff = min(backoff * 2, self._max_backoff)

    def _reset_failure_log_suppression(self) -> None:
        self._failure_log_window_started = None
        self._failure_log_suppressed = 0

    def _log_disconnect(
        self,
        exc: BaseException | None,
        lived: float | None,
        retry_delay: float | None,
    ) -> None:
        """Emit one bounded lifecycle warning without logging wire content."""
        code = getattr(exc, "code", None)
        reason = _safe_log_text(getattr(exc, "reason", None))
        detail = (
            "never-served" if lived is None else f"{lived:.3f}s",
            code,
            reason,
            type(exc).__name__ if exc is not None else "clean-exit",
            _safe_log_text(exc),
            "none" if retry_delay is None else f"{retry_delay:.3f}s",
        )
        now = time.monotonic()
        started = self._failure_log_window_started
        if started is not None:
            self._failure_log_suppressed += 1
            if now - started < _LIFECYCLE_LOG_WINDOW_S:
                return
            logger.warning(
                "tunnel disconnects suppressed=%d latest_lived=%s "
                "latest_close_code=%s latest_reason=%r latest_err=%s:%s "
                "latest_retry_delay=%s",
                self._failure_log_suppressed,
                *detail,
            )
            self._failure_log_window_started = now
            self._failure_log_suppressed = 0
            return

        logger.warning(
            "tunnel disconnected: lived=%s close_code=%s reason=%r "
            "err=%s:%s retry_delay=%s",
            *detail,
        )
        self._failure_log_window_started = now

    async def _handshake(self, ws) -> None:
        """Authenticate a fresh tunnel. The peer-relay park connector
        (``peer.PeerParkConnector``) overrides this to first demand the
        relay's own ``relay:serve`` proof before presenting a hello."""
        if self._machine_key is not None:
            await ws.send(build_tunnel_hello_v2(
                self._key, self._cert, machine_key=self._machine_key,
                org=self._org, ts=int(time.time()), caps=self._caps,
            ))
            expected_version = HELLO_VERSION_2
        else:
            await ws.send(build_tunnel_hello(
                self._key, self._cert, org=self._org, ts=int(time.time())
            ))
            expected_version = HELLO_VERSION
        reply = json.loads(await ws.recv())
        if not isinstance(reply, dict):
            raise ConnectionError(f"hello rejected: {reply!r}")
        if reply.get("ok") is True:
            remote_version = reply.get("v")
            if type(remote_version) is not int or remote_version != expected_version:
                raise TunnelProtocolVersionError(remote_version)
            accepted = reply.get("caps", [])
            self.accepted_caps = (
                tuple(accepted) if isinstance(accepted, list) else ()
            )
            return
        error = reply.get("error")
        if isinstance(error, dict) and error.get("code") == "protocol_version_mismatch":
            raise TunnelProtocolVersionError(error.get("registry_version"))
        else:
            raise ConnectionError(f"hello rejected: {reply!r}")

    async def _serve(self, ws) -> None:
        send_lock = asyncio.Lock()
        channels: dict = {}  # channel_id -> asyncio.Queue
        tasks: dict = {}

        async def send_frame(frame_type: int, channel_id: bytes, payload: bytes = b"") -> None:
            async with send_lock:
                await ws.send(encode_frame(frame_type, channel_id, payload))

        # Publish the send hook so control() can emit on this live tunnel;
        # cleared in the finally so a call between tunnels fails closed.
        self._ctrl_send = send_frame
        # Same lifetime for the push seam: one shared send_frame per
        # tunnel, so a publish addressed by token leaves exactly once.
        if self._publisher is not None:
            self._publisher.bind(send_frame)
        # Raw-stream adapter (tls-stream/1): live only when the operator
        # wired a handler AND this tunnel's ack negotiated the capability —
        # a non-negotiated tunnel can never carry a stream frame.
        adapter = None
        if (
            self._stream_handler is not None
            and CAP_TLS_STREAM in self.accepted_caps
        ):
            adapter = StreamAdapter(
                self._stream_handler, send_frame,
                lease_lookup=self._desired_hosts.get,
            )
        open_tasks: set = set()

        def drop(channel_id: bytes) -> None:
            queue = channels.pop(channel_id, None)
            if queue is not None:
                queue.put_nowait(None)  # poison pill for the channel task

        try:
            async for raw in ws:
                if isinstance(raw, str):
                    continue
                try:
                    frame = decode_frame(raw)
                except FrameError:
                    break
                if frame.type == FRAME_CTRL:
                    self._resolve_ctrl_reply(frame.payload)
                    continue
                if frame.type == FRAME_STREAM_CTRL:
                    if adapter is not None:
                        adapter.dispatch_ctrl(frame.channel_id, frame.payload)
                    continue  # never negotiated: stale/hostile frame, ignored
                if frame.type == FRAME_OPEN:
                    try:
                        meta = json.loads(frame.payload)
                        if (
                            isinstance(meta, dict)
                            and meta.get("kind") == "tls-stream"
                        ):
                            if adapter is None:
                                await send_frame(
                                    FRAME_CLOSE, frame.channel_id
                                )
                                continue
                            # Own task: a slow local dial must not stall
                            # the tunnel serve loop.
                            task = asyncio.create_task(
                                adapter.open(frame.channel_id, meta)
                            )
                            open_tasks.add(task)
                            task.add_done_callback(open_tasks.discard)
                            continue
                        if (
                            isinstance(meta, dict)
                            and meta.get("kind") == "host-probe"
                        ):
                            # One-shot routing diagnostic (auto-0zdky): echo
                            # which connector this hostname resolved to —
                            # a machine-key digest, never the key itself.
                            # The PROBER closes after reading; sending CLOSE
                            # here would race the relay's bounded writer
                            # into discarding the still-queued echo. No
                            # channel state is created.
                            await send_frame(
                                FRAME_DATA,
                                frame.channel_id,
                                json.dumps({
                                    "kind": "host-probe",
                                    "host": meta.get("host", ""),
                                    "reservation": meta.get(
                                        "reservation", ""
                                    ),
                                    "machine_digest": self._machine_digest(),
                                }).encode("utf-8"),
                            )
                            continue
                        token = meta["token"]
                    except (ValueError, KeyError, TypeError):
                        await send_frame(FRAME_CLOSE, frame.channel_id)
                        continue
                    queue: asyncio.Queue = asyncio.Queue()
                    channels[frame.channel_id] = queue
                    if self._publisher is not None:
                        self._publisher.attached(token)
                    tasks[frame.channel_id] = asyncio.create_task(
                        self._serve_channel(frame.channel_id, token, queue, send_frame, drop)
                    )
                elif frame.type == FRAME_DATA:
                    if adapter is not None and adapter.dispatch_data(
                        frame.channel_id, frame.payload
                    ):
                        continue
                    queue = channels.get(frame.channel_id)
                    if queue is not None:
                        queue.put_nowait(frame.payload)
                elif frame.type == FRAME_CLOSE:
                    if adapter is not None and adapter.dispatch_close(
                        frame.channel_id
                    ):
                        continue
                    drop(frame.channel_id)
        finally:
            if adapter is not None:
                for task in list(open_tasks):
                    task.cancel()
                await adapter.shutdown()
            self._ctrl_send = None
            if self._publisher is not None:
                self._publisher.unbind()
            # Fail any in-flight control calls rather than let them hang to
            # timeout — the tunnel that would carry their reply is gone.
            for correlation, future in list(self._pending.items()):
                if not future.done():
                    future.set_exception(
                        ConnectionError("tunnel dropped before control reply"))
                self._pending.pop(correlation, None)
            for channel_id in list(channels):
                drop(channel_id)
            for task in tasks.values():
                task.cancel()
            for task in tasks.values():
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    async def _serve_channel(self, channel_id: bytes, token: str,
                             queue: asyncio.Queue, send_frame, drop) -> None:
        """One viewer channel: handshake, then request/response messages."""
        try:
            await serve_channel(
                self._key, self._channel_cert, org=self._org, token=token,
                recv=queue.get,
                send=lambda data: send_frame(FRAME_DATA, channel_id, data),
                handler=self._handler,
            )
        except Exception:  # HandshakeError, RecordError, transport failures
            with contextlib.suppress(Exception):
                await send_frame(FRAME_CLOSE, channel_id)
        else:
            # Normal completion can be server-initiated (the bounded ICE
            # exchange is the first case). Tell the registry to close its
            # viewer side instead of relying on a cooperative viewer to do it.
            with contextlib.suppress(Exception):
                await send_frame(FRAME_CLOSE, channel_id)
        finally:
            # Always deregister, on every exit path, so no publish ever
            # targets a token whose last channel is gone.
            if self._publisher is not None:
                self._publisher.detached(token)
            drop(channel_id)


def main() -> None:
    parser = argparse.ArgumentParser(description="auto.network dashboard tunnel connector")
    parser.add_argument("--relay", required=True, help="relay base URL, e.g. ws://127.0.0.1:8477")
    parser.add_argument("--org", required=True)
    parser.add_argument("--key-file", required=True, help="file holding the private key hex")
    parser.add_argument("--cert-file", required=True, help="file holding the cert wire JSON")
    parser.add_argument(
        "--channel-cert-file",
        help=("identity-neutral certificate for viewer SERVER_HELLO; defaults "
              "to --cert-file for generic/test connectors"),
    )
    parser.add_argument("--mode", choices=["echo", "serve-file"], default="echo")
    parser.add_argument("--file", help="file to serve (serve-file mode)")
    parser.add_argument("--content-type", default="text/html")
    parser.add_argument("--min-backoff", type=float, default=0.2)
    parser.add_argument("--max-backoff", type=float, default=5.0)
    args = parser.parse_args()

    with open(args.key_file) as fh:
        key = KeyPair.from_private_hex(fh.read().strip())
    with open(args.cert_file) as fh:
        cert = DelegationCert.from_json(fh.read().strip())
    channel_cert = cert
    if args.channel_cert_file:
        with open(args.channel_cert_file) as fh:
            channel_cert = DelegationCert.from_json(fh.read().strip())

    if args.mode == "serve-file":
        if not args.file:
            parser.error("--mode serve-file requires --file")
        handler = file_handler(args.file, args.content_type)
    else:
        handler = echo_handler

    connector = TunnelConnector(
        args.relay, args.org, key, cert, handler, channel_cert=channel_cert,
        min_backoff=args.min_backoff, max_backoff=args.max_backoff,
    )
    asyncio.run(connector.run())


if __name__ == "__main__":
    main()
