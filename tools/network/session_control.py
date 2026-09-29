"""session-control/1: remote session control between the operator's machines.

Connector-side half (graph://7eb29bc8-31a §9.1, bead auto-99ioi). A
session-control pair is a relay-brokered directed pair of its own capability;
inside it the fleet handshake runs with a ``session:control`` delegation
(FleetAuthenticator(scope="session:control")), and then each pair carries
exactly one request and its reply as JSON records::

    request  {"v": 1, "op": "<op>", "body": {...}}
    reply    {"v": 1, "ok": true, "result": {...}}
           | {"v": 1, "ok": false, "refusal": "<typed reason>", "detail": "..."}

The channel lives in this connector process, but the operations belong to the
dashboard (its lifecycle worker, its session store). Rather than a new
connector-to-dashboard listener or credential, the dashboard's existing
authenticated control socket is used in both directions:

* OUTBOUND: the dashboard calls ctl ``session-control-request``; this process
  resolves the peer's slot, opens the pair, authenticates, sends the request
  and returns the reply (:func:`request`).
* INBOUND: an accepted pair's request is parked in :data:`inbound`; the
  dashboard long-polls ctl ``session-control-next``, executes the op itself,
  and answers with ``session-control-reply`` (:class:`InboundBroker`).

The destination is resolved from the ROSTER by durable machine_pub, never
from a presence row or a display name.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import time
from typing import Any, Awaitable, Callable, Optional

from tools.network.fleet_process_scope import SESSION_CONTROL_SCOPE, scope_problem
from tools.network.fleet_sync_channel import (
    HANDSHAKE_REFUSALS,
    FleetAuthenticator,
    FleetHandshakeRefused,
    authenticate_fleet_transport,
    serve_fleet_transport,
)
from tools.network.relaykit.frames import VIEWER_KIND_RECORD, tag_viewer_message
from tools.network.relaykit.viewer import read_viewer_record
from tools.network.relaykit.fleet_stream import (
    FleetStreamClosed,
    FleetStreamEndpoint,
    FleetStreamRefused,
)

logger = logging.getLogger("fleet.session_control")

SESSION_CONTROL_VERSION = 1
#: Largest request or reply body, JSON-encoded.
MAX_RECORD_BYTES = 256 * 1024
#: How long an inbound request waits for the dashboard to answer it.
INBOUND_REPLY_TIMEOUT_S = 30.0
#: Bound on requests parked for the dashboard; beyond it a request is refused.
INBOUND_QUEUE_LIMIT = 64

#: A reply may stream one local file after its JSON header
#: (graph://7eb29bc8-31a §9.2 ``output`` / ``fetch-branch``): records cap at
#: MAX_RECORD_BYTES, so the dashboard names a file and this process streams
#: it in chunks. Bounded per transfer.
STREAM_CHUNK_BYTES = 192 * 1024
MAX_STREAM_BYTES = 64 * 1024 * 1024
#: Ceiling on one whole transfer, whatever its size.
TRANSFER_DEADLINE_CAP_S = 600
#: Seconds an incoming transfer file may sit before a later transfer sweeps it.
TRANSFER_RETENTION_S = 3600

# Typed refusals (graph://7eb29bc8-31a §9.2). One code per failure path, so
# a refusal names the check that failed. Relay admission reasons arrive
# verbatim from the relay; handshake checks carry FleetHandshakeRefused's
# own-*/peer-* codes. Every refusal record also says WHERE it was decided:
# ``at: "local"`` (the requesting machine) or ``at: "peer"`` (the target).
#
# This machine, before any pair is opened:
NOT_NEGOTIATED = "session-control-not-negotiated"   # relay offered no session-control/1
UNARMED = "session-control-unarmed"                 # no fleet runtime armed here
NOT_GRANTED = "session-control-not-granted"         # our delegation lacks session:control
NOT_IN_ROSTER = "peer-not-in-roster"
SLOT_ABSENT = "destination-slot-absent"
SLOT_LOOKUP_FAILED = "slot-lookup-failed"           # the relay's slot list could not be read
REQUEST_TOO_LARGE = "request-too-large"
# The pair, after the relay admitted it:
PEER_CLOSED_AT_OPEN = "peer-closed-at-open"         # peer connector declined the pair
PEER_CLOSED_IN_HANDSHAKE = "peer-closed-in-handshake"  # closed with no typed refusal
HANDSHAKE_TIMEOUT = "handshake-timeout"
REPLY_TIMEOUT = "reply-timeout"
REPLY_NOT_JSON = "reply-not-json"
REPLY_MALFORMED = "reply-malformed"
STREAM_CHUNK_TIMEOUT = "stream-chunk-timeout"
TRANSFER_DEADLINE = "transfer-deadline-exceeded"
STREAM_TOO_LARGE = "stream-too-large"
STREAM_SHORT = "stream-ended-short"
FAILED = "session-control-failed"                   # an exception no path above names
# The target, answering:
DASHBOARD_UNAVAILABLE = "session-control-dashboard-unavailable"
BUSY = "session-control-busy"
INCOMING_TOO_LARGE = "incoming-request-too-large"
REQUEST_NOT_JSON = "request-not-json"
REQUEST_MALFORMED = "request-malformed"
REPLY_TOO_LARGE = "reply-too-large"
FILE_NOT_STREAMABLE = "file-not-streamable"
CTL_REQUEST_MALFORMED = "ctl-request-malformed"     # dashboard -> connector bridge
#: A pre-handshake refusal record whose code is not one a target can send.
PEER_REFUSED = "peer-refused"
#: What a target may say in place of its hello: its own not-armed/not-granted
#: state, or a handshake check it ran on our hello. The record is
#: unauthenticated, so nothing else is believed (it could steer the UI).
PEER_REFUSAL_CODES = frozenset({UNARMED, NOT_GRANTED}) | HANDSHAKE_REFUSALS
#: Kept for callers that size their own payloads (send text, output files).
OP_TOO_LARGE = "op-too-large"


class SessionControlError(Exception):
    """A request that did not produce a reply, with its typed reason."""

    def __init__(self, refusal: str, detail: str = ""):
        super().__init__(f"{refusal}: {detail}" if detail else refusal)
        self.refusal = refusal
        self.detail = detail


def encode(record: dict, *, too_large: str = REPLY_TOO_LARGE) -> bytes:
    data = json.dumps(record, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(data) > MAX_RECORD_BYTES:
        raise SessionControlError(too_large, f"{len(data)} bytes")
    return data


def refusal(reason: str, detail: str = "", *, at: str | None = None) -> dict:
    out = {"v": SESSION_CONTROL_VERSION, "ok": False, "refusal": reason}
    if detail:
        out["detail"] = detail[:300]
    if at is not None:
        out["at"] = at
    return out


def _is_refusal_record(data: object) -> bool:
    """A pre-handshake refusal the target sends in place of its hello."""
    return (isinstance(data, dict) and data.get("v") == SESSION_CONTROL_VERSION
            and data.get("ok") is False and isinstance(data.get("refusal"), str)
            and set(data) <= {"v", "ok", "refusal", "detail"})


def parse_request(raw: bytes) -> dict:
    if len(raw) > MAX_RECORD_BYTES:
        raise SessionControlError(INCOMING_TOO_LARGE, f"{len(raw)} bytes")
    try:
        request = json.loads(raw)
    except ValueError as exc:
        raise SessionControlError(REQUEST_NOT_JSON, "not JSON") from exc
    if (
        not isinstance(request, dict)
        or request.get("v") != SESSION_CONTROL_VERSION
        or not isinstance(request.get("op"), str)
        or not isinstance(request.get("body", {}), dict)
    ):
        raise SessionControlError(REQUEST_MALFORMED, "not a session-control request")
    return {"op": request["op"], "body": request.get("body") or {}}


def _data_root():
    from pathlib import Path

    from tools.data_paths import DATA_ROOT

    return Path(DATA_ROOT)


def transfer_dir():
    """Where streamed transfers land and where the dashboard stages the
    files it asks this process to stream."""
    path = _data_root() / "session-transfer"
    path.mkdir(parents=True, exist_ok=True)
    return path


def stream_path_allowed(path) -> bool:  # a check only; streaming uses open_streamable
    """A file the dashboard may ask this process to stream: a regular file
    under data/agent-runs or data/host-uploads (session output) or
    data/session-transfer (staged transfers)."""
    from pathlib import Path

    try:
        resolved = Path(path).resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    if not resolved.is_file():
        return False
    root = _data_root().resolve()
    for allowed in (root / "agent-runs", root / "host-uploads",
                    root / "session-transfer"):
        try:
            resolved.relative_to(allowed)
            return True
        except ValueError:
            continue
    return False


def _sweep_transfers(now: float | None = None) -> None:
    now = time.time() if now is None else now
    with contextlib.suppress(OSError):
        for entry in transfer_dir().iterdir():
            with contextlib.suppress(OSError):
                if entry.is_file() and now - entry.stat().st_mtime > TRANSFER_RETENTION_S:
                    entry.unlink()


def _within_roots(path) -> bool:
    from pathlib import Path

    root = _data_root().resolve()
    for allowed in (root / "agent-runs", root / "host-uploads",
                    root / "session-transfer"):
        try:
            Path(path).relative_to(allowed)
            return True
        except ValueError:
            continue
    return False


def open_streamable(path) -> tuple[int, int] | None:
    """Open *path* ONCE for streaming and prove what was opened.

    The session controls its own output tree, so a check on a path followed
    by an open of that path is a race it can win by swapping in a symlink.
    Instead: resolve, refuse a final-component symlink (O_NOFOLLOW), then
    check the OPEN descriptor -- its real path (/proc/self/fd) must lie under
    an allowed root, it must be a regular file no larger than
    MAX_STREAM_BYTES. Returns ``(fd, size)``, the size read from the fd, or
    None; the caller owns closing the fd.
    """
    import stat
    from pathlib import Path

    try:
        resolved = Path(path).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not _within_roots(resolved):
        return None
    try:
        # O_NONBLOCK: a FIFO planted in a session's output would otherwise
        # block this open (and the event loop) until someone writes to it;
        # non-blocking it returns at once and S_ISREG below refuses it.
        fd = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                     | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        real = Path(os.readlink(f"/proc/self/fd/{fd}"))
        if (not stat.S_ISREG(info.st_mode) or info.st_size > MAX_STREAM_BYTES
                or not _within_roots(real)):
            os.close(fd)
            return None
    except OSError:
        os.close(fd)
        return None
    return fd, info.st_size


async def _stream_reply(header: dict, fd: int, size: int, path, delete: bool):
    """The streamed reply: the JSON header, then exactly *size* bytes read
    from the already-verified descriptor *fd*."""
    try:
        yield encode(header)
        remaining = size
        while remaining > 0:
            chunk = await asyncio.to_thread(
                os.read, fd, min(STREAM_CHUNK_BYTES, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
    finally:
        os.close(fd)
        if delete:
            with contextlib.suppress(OSError):
                os.unlink(path)


def session_authenticator(runtime) -> FleetAuthenticator:
    """The session:control authenticator for an ARMED connector runtime.

    Same process key, root, roster and delegation as the sync authenticator:
    one delegation carries both scopes (fleet_process_scope). Only the scope
    this authenticator verifies differs.
    """
    scheduler = getattr(runtime, "scheduler", None)
    if scheduler is None:
        raise SessionControlError(UNARMED, "this process is not armed")
    sync = scheduler.authenticator
    cert = sync.delegation_cert
    if cert is None or scope_problem(cert.scope, SESSION_CONTROL_SCOPE) is not None:
        held = list(cert.scope) if cert is not None else []
        raise SessionControlError(
            NOT_GRANTED,
            f"this runtime's delegation scope {held} does not include "
            f"{SESSION_CONTROL_SCOPE!r}; it was armed before the scope existed "
            f"or without it")
    return FleetAuthenticator(
        sync.machine_key,
        root_pub=sync.root_pub,
        roster_entries=sync._roster_entries,
        roster_machine_pub=sync.machine_pub,
        delegation_cert=cert,
        require_delegation=True,
        scope=SESSION_CONTROL_SCOPE,
    )


# ── inbound: pairs another machine opened to this one ─────────────────────────


class InboundBroker:
    """Requests from peers, parked until the dashboard answers them."""

    def __init__(self, *, limit: int = INBOUND_QUEUE_LIMIT,
                 reply_timeout: float = INBOUND_REPLY_TIMEOUT_S):
        self._queue: asyncio.Queue = asyncio.Queue()
        self._waiting: dict[str, asyncio.Future] = {}
        self._limit = limit
        self._reply_timeout = reply_timeout

    async def submit(self, op: str, body: dict, *, peer_machine_pub: str) -> dict:
        """Park one request and wait for the dashboard's reply."""
        if len(self._waiting) >= self._limit:
            return refusal(BUSY, "too many requests waiting for the dashboard")
        request_id = secrets.token_hex(16)
        future = asyncio.get_running_loop().create_future()
        self._waiting[request_id] = future
        self._queue.put_nowait({
            "id": request_id, "op": op, "body": body,
            "peer_machine_pub": peer_machine_pub, "received_at": time.time(),
        })
        try:
            return await asyncio.wait_for(future, self._reply_timeout)
        except asyncio.TimeoutError:
            return refusal(DASHBOARD_UNAVAILABLE,
                           "the dashboard did not answer in time")
        finally:
            self._waiting.pop(request_id, None)

    async def next(self, wait_s: float) -> Optional[dict]:
        """The next request whose submitter is still waiting, or None."""
        deadline = asyncio.get_running_loop().time() + max(0.0, wait_s)
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return None
            try:
                item = await asyncio.wait_for(self._queue.get(), remaining)
            except asyncio.TimeoutError:
                return None
            if item["id"] in self._waiting:
                return item

    def reply(self, request_id: str, reply: dict) -> bool:
        future = self._waiting.get(request_id)
        if future is None or future.done():
            return False
        future.set_result(reply)
        return True


#: This connector process's broker; the ctl ops read and answer it.
inbound = InboundBroker()


def session_control_offer_handler(
    runtime, broker: InboundBroker = inbound,
) -> Callable[[FleetStreamEndpoint], Awaitable[bool]]:
    """The connector's ``session_control_offer``: accept an inbound pair only
    while this process holds a session:control credential, then serve the
    session:control handshake and one request on it."""
    tasks: set = set()

    async def on_offer(endpoint: FleetStreamEndpoint) -> bool:
        try:
            authenticator = session_authenticator(runtime)
        except SessionControlError as exc:
            # Accept the pair only to say why, in place of a hello: a bare
            # decline reaches the requester as a reset with no reason.
            logger.info("session-control offer refused: %s", exc.refusal)
            task = asyncio.create_task(
                _refuse_pair(endpoint, exc.refusal, exc.detail))
        else:
            task = asyncio.create_task(_serve(endpoint, authenticator, broker))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return True

    return on_offer


#: How long a refused pair waits for the requester to read the refusal.
REFUSAL_LINGER_S = 5.0


async def _send_refusal(endpoint: FleetStreamEndpoint, code: str, detail: str) -> None:
    """Send the pre-handshake refusal record, then half-close and linger so
    the requester reads it before the pair resets. Unauthenticated by
    nature (no channel exists yet): it can only report a refusal, never
    grant anything, and the requester marks it ``at: "peer"``."""
    record = encode(refusal(code, detail))
    with contextlib.suppress(Exception):
        await endpoint.send(tag_viewer_message(VIEWER_KIND_RECORD, record))
        await endpoint.half_close()
        await asyncio.wait_for(endpoint.closed.wait(), REFUSAL_LINGER_S)


async def _refuse_pair(endpoint: FleetStreamEndpoint, code: str, detail: str) -> None:
    try:
        await endpoint.ready.wait()
        if not endpoint.closed.is_set():
            await _send_refusal(endpoint, code, detail)
    finally:
        with contextlib.suppress(Exception):
            await endpoint.close()


async def _serve(endpoint: FleetStreamEndpoint, authenticator: FleetAuthenticator,
                 broker: InboundBroker) -> None:
    await endpoint.ready.wait()
    if endpoint.closed.is_set():
        return
    hello_sent = False

    async def send(message: bytes) -> None:
        # The first message out is the server hello; once it is sent a
        # handshake refusal can no longer be reported in its place.
        nonlocal hello_sent
        hello_sent = True
        await endpoint.send(message)

    async def handler(_token, message, client_pub, **_extra):
        # The sender is the machine the handshake proved, never a field of
        # the request.
        try:
            request = parse_request(message)
        except SessionControlError as exc:
            return encode(refusal(exc.refusal, exc.detail))
        reply = await broker.submit(
            request["op"], request["body"], peer_machine_pub=client_pub)
        result = reply.get("result") if reply.get("ok") else None
        if isinstance(result, dict) and "stream_file" in result:
            result = dict(result)
            path = result.pop("stream_file")
            delete = bool(result.pop("stream_delete", False))
            opened = open_streamable(path)
            if opened is None:
                if delete:
                    with contextlib.suppress(OSError):
                        os.unlink(path)
                return encode(refusal(FILE_NOT_STREAMABLE, "the file cannot be streamed"))
            fd, size = opened
            header = {**reply, "result": {**result, "stream": {"size": size}}}
            return _stream_reply(header, fd, size, path, delete)
        try:
            return encode(reply)
        except SessionControlError as exc:
            return encode(refusal(exc.refusal, exc.detail))

    async def close(**_kwargs):
        await endpoint.close()

    try:
        await serve_fleet_transport(
            token=endpoint.session, recv=endpoint.recv, send=send,
            handler=handler, close=close, authenticator=authenticator,
        )
    except FleetHandshakeRefused as exc:
        # The requester's hello failed one of our checks: tell it which.
        logger.info("session-control hello refused: %s", exc.refusal)
        if not hello_sent and not endpoint.closed.is_set():
            await _send_refusal(endpoint, exc.refusal, exc.detail)
    except (FleetStreamClosed, ConnectionError):
        pass
    except Exception:
        logger.warning("session-control pair %s ended with an error",
                       endpoint.pair_id[:8], exc_info=True)
    finally:
        with contextlib.suppress(Exception):
            await endpoint.close()


# ── outbound: this machine asks another ───────────────────────────────────────


async def _receive_stream(channel, timeout: float) -> dict:
    """Read a (possibly streamed) reply: the header, then any file chunks
    into a transfer file whose path is returned as ``result.file``."""
    import hashlib

    stream = channel.recv_message_stream()
    try:
        first, final = await asyncio.wait_for(stream.__anext__(), timeout)
    except asyncio.TimeoutError:
        raise SessionControlError(REPLY_TIMEOUT, f"no reply within {timeout}s") from None
    header = _decode_reply(first)
    announced = ((header.get("result") or {}).get("stream") or {}).get("size")
    if final or not header.get("ok") or announced is None:
        return header
    _sweep_transfers()
    target = transfer_dir() / f"in-{secrets.token_hex(16)}"
    digest, size = hashlib.sha256(), 0
    # One deadline for the whole transfer, not only per chunk: a sender that
    # drips a chunk just inside the per-chunk timeout must not hold the pair
    # and its budget open indefinitely. timeout + 1 s per 64 KiB, capped.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + min(
        timeout + int(announced) / (64 * 1024), TRANSFER_DEADLINE_CAP_S)
    try:
        with open(target, "wb") as fh:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise SessionControlError(
                        TRANSFER_DEADLINE, "transfer exceeded its deadline")
                try:
                    chunk, final = await asyncio.wait_for(
                        stream.__anext__(), min(timeout, remaining))
                except asyncio.TimeoutError:
                    if loop.time() >= deadline:
                        raise SessionControlError(
                            TRANSFER_DEADLINE,
                            "transfer exceeded its deadline") from None
                    raise SessionControlError(
                        STREAM_CHUNK_TIMEOUT,
                        f"no stream chunk within {min(timeout, remaining):.0f}s") from None
                size += len(chunk)
                if size > MAX_STREAM_BYTES or size > int(announced):
                    raise SessionControlError(STREAM_TOO_LARGE, "stream exceeds its size")
                digest.update(chunk)
                await asyncio.to_thread(fh.write, chunk)
                if final:
                    break
        if size != int(announced):
            raise SessionControlError(STREAM_SHORT, "stream ended short")
    except BaseException:
        with contextlib.suppress(OSError):
            target.unlink()
        raise
    header["result"] = {**header["result"], "file": str(target),
                        "sha256": digest.hexdigest()}
    return header


def _decode_reply(raw: bytes) -> dict:
    try:
        reply = json.loads(raw)
    except ValueError as exc:
        raise SessionControlError(REPLY_NOT_JSON, str(exc)) from exc
    if not isinstance(reply, dict) or reply.get("v") != SESSION_CONTROL_VERSION:
        raise SessionControlError(REPLY_MALFORMED, "not a session-control reply")
    return reply


class _RefusalAwareTransport:
    """The pair as the handshake's transport, recognising the typed refusal
    a target sends in place of its hello (see ``_send_refusal``)."""

    def __init__(self, endpoint: FleetStreamEndpoint):
        self._endpoint = endpoint
        self.session = endpoint.session
        self._first = True

    async def send(self, message: bytes) -> None:
        await self._endpoint.send(message)

    async def recv(self):
        raw = await self._endpoint.recv()
        if self._first:
            # Only the message in the hello's place can be a refusal; every
            # later record is channel ciphertext.
            self._first = False
            data = None
            if isinstance(raw, (bytes, bytearray)):
                try:
                    data = json.loads(read_viewer_record(bytes(raw)))
                except Exception:
                    data = None
            if _is_refusal_record(data):
                code = data["refusal"]
                detail = str(data.get("detail", ""))[:300]
                if code not in PEER_REFUSAL_CODES:
                    detail = f"unrecognised peer refusal {code[:64]!r}"
                    code = PEER_REFUSED
                raise _PeerRefusal(code, detail)
        return raw

    async def close(self) -> None:
        await self._endpoint.close()


class _PeerRefusal(SessionControlError):
    """A refusal the target reported, rather than one decided here."""


async def request(connector, runtime, *, machine_pub: str, op: str,
                  body: dict, timeout: float = 15.0,
                  resolve_slot=None, stream: bool = False) -> dict:
    """Send one request to the fleet machine *machine_pub* and return its
    reply record. Every failure is a typed refusal record, never a raise, so
    an old relay ("unknown control op") or an unarmed peer reads the same way
    as any other refusal. A refusal carries ``at``: ``local`` when this
    machine decided it, ``peer`` when the target did."""
    try:
        record = encode({"v": SESSION_CONTROL_VERSION, "op": op, "body": body},
                        too_large=REQUEST_TOO_LARGE)
        adapter = getattr(connector, "session_streams", None)
        if adapter is None:
            # The relay did not negotiate session-control/1 on this tunnel,
            # which is what a relay that predates it looks like.
            raise SessionControlError(
                NOT_NEGOTIATED, "the relay did not negotiate session-control/1")
        authenticator = session_authenticator(runtime)
        if resolve_slot is None:
            from tools.network.fleet_relay_carrier import resolve_peer_slot
            resolve_slot = resolve_peer_slot
        try:
            (persona_pub, slot_machine), _source = await resolve_slot(
                connector, runtime, machine_pub, timeout=timeout)
        except ConnectionError as exc:
            # PeerSlotError names its reason; any other ConnectionError is
            # the relay's slot list failing to load.
            raise SessionControlError(
                getattr(exc, "reason", None) or SLOT_LOOKUP_FAILED, str(exc)) from exc
        try:
            endpoint = await adapter.open(
                persona_pub, slot_machine,
                claimed_machine_pub=authenticator.machine_pub, timeout=timeout)
        except FleetStreamRefused as exc:
            # The relay's own admission reason, verbatim; a relay that
            # predates session-control/1 says "unknown control op".
            raise SessionControlError(exc.reason) from exc
        except FleetStreamClosed as exc:
            # Admitted by the relay, then declined by the peer's connector
            # without a word: a target that predates typed refusals.
            raise SessionControlError(PEER_CLOSED_AT_OPEN, str(exc)) from exc
        try:
            try:
                channel = await asyncio.wait_for(authenticate_fleet_transport(
                    _RefusalAwareTransport(endpoint), authenticator=authenticator,
                    expected_machine_pub=machine_pub, session=endpoint.session,
                ), timeout)
            except asyncio.TimeoutError:
                raise SessionControlError(
                    HANDSHAKE_TIMEOUT, f"no handshake within {timeout}s") from None
            except FleetStreamClosed as exc:
                raise SessionControlError(PEER_CLOSED_IN_HANDSHAKE, str(exc)) from exc
            await channel.send_message(record)
            if stream:
                reply = await _receive_stream(channel, timeout)
            else:
                try:
                    raw = await asyncio.wait_for(channel.recv_message(), timeout)
                except asyncio.TimeoutError:
                    raise SessionControlError(
                        REPLY_TIMEOUT, f"no reply within {timeout}s") from None
                reply = _decode_reply(raw)
        finally:
            with contextlib.suppress(Exception):
                await endpoint.close()
        if reply.get("ok") is False and "at" not in reply:
            reply = {**reply, "at": "peer"}
        return reply
    except _PeerRefusal as exc:
        return refusal(exc.refusal, exc.detail, at="peer")
    except SessionControlError as exc:
        return refusal(exc.refusal, exc.detail, at="local")
    except FleetHandshakeRefused as exc:
        # Our check of the target's hello failed; the code says which check.
        return refusal(exc.refusal, exc.detail, at="local")
    except Exception as exc:  # nothing above names it
        return refusal(FAILED, f"{type(exc).__name__}: {exc}", at="local")


def ctl_op(op: str) -> bool:
    return op in ("session-control-request", "session-control-next",
                  "session-control-reply")


async def handle_ctl(connector, runtime, op: str, args: Any,
                     broker: InboundBroker = inbound) -> dict:
    """The three ctl ops the dashboard drives this module with."""
    args = args if isinstance(args, dict) else {}
    if op == "session-control-request":
        machine_pub, request_op = args.get("machine_pub"), args.get("op")
        if not isinstance(machine_pub, str) or not isinstance(request_op, str):
            return {"ok": True, "reply": refusal(
                CTL_REQUEST_MALFORMED, "machine_pub and op are required", at="local")}
        reply = await request(
            connector, runtime, machine_pub=machine_pub, op=request_op,
            body=args.get("body") if isinstance(args.get("body"), dict) else {},
            timeout=float(args.get("timeout") or 15.0),
            stream=bool(args.get("stream")),
        )
        return {"ok": True, "reply": reply}
    if op == "session-control-next":
        item = await broker.next(float(args.get("wait") or 20.0))
        return {"ok": True, "request": item}
    if op == "session-control-reply":
        request_id, reply = args.get("id"), args.get("reply")
        if not isinstance(request_id, str) or not isinstance(reply, dict):
            return {"ok": False, "error": "id and reply are required"}
        return {"ok": True, "delivered": broker.reply(request_id, reply)}
    return {"ok": False, "error": f"unknown session-control ctl op {op!r}"}
