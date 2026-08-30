"""Fleet-authenticated checkpoint pull over an ordinary RelayKit link.

The outer ViewerChannel already supplies encrypted, integrity-protected
transport pinned to the serving organization. Fleet authentication is a
separate application boundary: each pull carries the existing signed Fleet
client hello and the response begins with the existing signed server hello.
No second encryption layer is added.

The initial alpha keeps the invitation link as the joining machine's local
route credential. The inner machine proof means possession of that bearer is
not Fleet authority; rotating it to a dedicated post-enrollment grant does not
change this protocol.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import sqlite3
import shutil
import struct
import tempfile
import time
import urllib.parse
from pathlib import Path, PurePosixPath

from tools.graph.db import _org_db_path
from tools.network import (
    fleet_roster,
    fleet_route,
    fleet_runtime,
    fleet_sync_telemetry,
)
from tools.network.fleet_sync_channel import FleetAuthenticator
from tools.network.fleet_sync_scheduler import (
    _DONE_MAGIC,
    _MUTATION_MAGIC,
    decode_authored,
    decode_done,
    encode_pull_request,
    FleetSyncRuntimeConfig,
    FleetSyncScheduler,
    SQLiteFleetSyncStore,
    dashboard_fleet_sync_service,
    roster_epoch,
)
from tools.network.fleet_sync.sync import FleetSyncAlpha
from tools.network.idkit import canonical_json
from tools.network.relaykit.viewer import ViewerChannel


logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
PULL_OP = "fleet.sync.pull"
CONTROL_OP = "fleet-runtime"
FILE_MAGIC = b"FSB1"
MAX_CHECKPOINT_FILES = 4096
MAX_CHECKPOINT_BYTES = 4 * 1024 * 1024 * 1024
_REQUEST_FIELDS = {
    "v", "op", "roster_epoch", "checkpoint", "after_transaction_ref", "hello",
}


class FleetRelaySyncError(RuntimeError):
    """The route, Fleet proof, or checkpoint stream was invalid."""


def _json(raw: object, what: str) -> dict:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise FleetRelaySyncError(f"{what} is not JSON") from exc
    if not isinstance(value, dict):
        raise FleetRelaySyncError(f"{what} must be an object")
    return value


def _encode_file(relative: str, body: bytes) -> bytes:
    header = canonical_json({
        "path": relative,
        "size": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
    })
    return FILE_MAGIC + struct.pack(">I", len(header)) + header + body


def _decode_file(raw: bytes) -> tuple[str, bytes]:
    if not isinstance(raw, bytes) or not raw.startswith(FILE_MAGIC) or len(raw) < 8:
        raise FleetRelaySyncError("checkpoint file frame is malformed")
    header_size = struct.unpack(">I", raw[4:8])[0]
    if header_size > 4096 or 8 + header_size > len(raw):
        raise FleetRelaySyncError("checkpoint file header is malformed")
    header = _json(raw[8:8 + header_size], "checkpoint file header")
    if set(header) != {"path", "size", "sha256"}:
        raise FleetRelaySyncError("checkpoint file header has unknown fields")
    relative = header["path"]
    path = PurePosixPath(relative) if isinstance(relative, str) else None
    if (
        path is None
        or path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise FleetRelaySyncError("checkpoint file path escapes its stage")
    body = raw[8 + header_size:]
    if header["size"] != len(body) or header["sha256"] != hashlib.sha256(body).hexdigest():
        raise FleetRelaySyncError("checkpoint file digest does not match")
    return path.as_posix(), body


class ConnectorFleetRuntime:
    """Short-lived Fleet server credential held only by the connector."""

    def __init__(self) -> None:
        self.scheduler: FleetSyncScheduler | None = None
        #: How many sync pulls this process has turned away because it holds no
        #: credential (scheduler is None), and when the first one arrived. This
        #: is the "764 requests refused since 8pm" the profile sync flag reports:
        #: an unarmed serving process looks identical to a healthy one on every
        #: other check, and the count is the difference between "nothing is
        #: happening" and "something is trying and failing". Reset by configure()
        #: — a fresh arm starts a new "since".
        self.locked_refusals: int = 0
        self.first_locked_refusal_at: float | None = None

    def configure(self, payload: object) -> dict:
        from tools.dashboard.link_approvals import _load_binding
        from tools.network import fleet_tunnel_server

        root_pub = fleet_tunnel_server._personal_root_pub()
        if root_pub is None:
            raise FleetRelaySyncError("connector has no personal Fleet anchor")
        entries = tuple(fleet_roster.load_entries(org=None))
        # The dashboard-side caller (_activate_runtime) resolves and passes
        # org_uuid when it builds its own copy of this same credential from
        # the same payload; this connector-side build was missing it, so a
        # payload carrying a reachability_cert (the normal case once the
        # personal org is registered) always failed
        # "reachability credential delivered without a registered org_uuid"
        # here even though the org WAS registered -- found live 2026-08-23.
        binding, _err = _load_binding(None)
        org_uuid = binding.get("org_uuid") if binding else None
        credential = fleet_runtime.FleetRuntimeCredential.from_browser_payload(
            payload,
            personal_root_pub=root_pub,
            roster_entries=entries,
            org_uuid=org_uuid,
        )
        config = FleetSyncRuntimeConfig(
            machine_key=credential.process_key,
            roster_machine_pub=credential.machine_pub,
            delegation_cert=credential.delegation_cert,
            require_delegation=True,
            personal_root_pub=root_pub,
            roster_entries=lambda: fleet_roster.load_entries(org=None),
            peer_addresses=lambda: {},
            personal_db_path=_org_db_path("personal"),
            telemetry_recorder=fleet_sync_telemetry.record_iteration,
        )
        scheduler = FleetSyncScheduler(config)
        scheduler._roster_snapshot = entries
        scheduler.authenticator.authorize(credential.machine_pub)
        self.scheduler = scheduler
        # A fresh credential means this process is no longer refusing — start a
        # new refusal tally so the flag's "since" reflects THIS lock, not one
        # cleared hours ago.
        self.locked_refusals = 0
        self.first_locked_refusal_at = None
        return {"ok": True, "machine_id": credential.machine_id}

    async def handle(self, token: str, message: dict):
        scheduler = self.scheduler
        if scheduler is None:
            self.locked_refusals += 1
            if self.first_locked_refusal_at is None:
                self.first_locked_refusal_at = time.time()
            raise FleetRelaySyncError("serving machine is locked for Fleet sync")
        if set(message) != _REQUEST_FIELDS or message.get("v") != PROTOCOL_VERSION \
                or message.get("op") != PULL_OP:
            raise FleetRelaySyncError("fleet sync pull has unknown fields")
        requested_epoch = message.get("roster_epoch")
        if not isinstance(requested_epoch, str) or len(requested_epoch) != 64:
            raise FleetRelaySyncError("fleet sync pull has no roster epoch")
        include_checkpoint = message.get("checkpoint")
        if not isinstance(include_checkpoint, bool):
            raise FleetRelaySyncError("fleet sync pull checkpoint flag must be bool")
        after_transaction_ref = message.get("after_transaction_ref")
        if (
            isinstance(after_transaction_ref, bool)
            or not isinstance(after_transaction_ref, int)
            or after_transaction_ref < 0
        ):
            raise FleetRelaySyncError("fleet sync pull position is malformed")
        hello = canonical_json(message.get("hello"))
        peer_pub, _private, server_hello, _transcript = (
            scheduler.authenticator.accept_client(hello, session=token)
        )
        current_epoch = scheduler._current_epoch()
        logger.warning(
            "fleet relay sync: accept_client ok, peer_pub=%s, entering stream",
            peer_pub[:16] if isinstance(peer_pub, str) else peer_pub,
        )

        async def stream():
            started_at_ns = time.time_ns()
            started_monotonic_ns = time.monotonic_ns()
            stats = {
                "bytes_sent": 0,
                "bytes_received": len(canonical_json(message)),
                "mutation_frames": 0,
                "transactions": 0,
                "checkpoint_bytes": 0,
            }
            outcome = "failed"
            error_code = "stream_incomplete"
            root = Path(tempfile.mkdtemp(prefix="fleet-relay-checkpoint-"))
            checkpoint = root / "checkpoint"
            try:
                server_hello_frame = canonical_json({
                    "v": PROTOCOL_VERSION,
                    "kind": "fleet.server-hello",
                    "hello": _json(server_hello, "fleet server hello"),
                    "roster_epoch": current_epoch,
                })
                stats["bytes_sent"] += len(server_hello_frame)
                yield server_hello_frame
                if include_checkpoint:
                    active = tuple(sorted(fleet_roster.resolve(
                        scheduler._roster_snapshot,
                        anchor_root_pub=scheduler.config.personal_root_pub,
                    )))

                    def create_checkpoint():
                        with FleetSyncAlpha(
                            scheduler.config.personal_db_path,
                            scheduler.authenticator.machine_pub,
                        ) as alpha:
                            alpha.checkpoint(
                                checkpoint,
                                roster_epoch=current_epoch,
                                active_roster=active,
                            )

                    await asyncio.to_thread(create_checkpoint)
                    files = tuple(sorted(
                        path for path in checkpoint.rglob("*") if path.is_file()
                    ))
                    total = sum(path.stat().st_size for path in files)
                    if len(files) > MAX_CHECKPOINT_FILES or total > MAX_CHECKPOINT_BYTES:
                        raise FleetRelaySyncError("checkpoint exceeds relay bounds")
                    begin = canonical_json({
                        "v": PROTOCOL_VERSION,
                        "kind": "checkpoint.begin",
                        "file_count": len(files),
                        "total_bytes": total,
                        "source_machine_pub": scheduler.authenticator.machine_pub,
                        "roster_epoch": current_epoch,
                    })
                    stats["bytes_sent"] += len(begin)
                    yield begin
                    for path in files:
                        scheduler.authenticator.authorize(peer_pub)
                        relative = path.relative_to(checkpoint).as_posix()
                        encoded_file = _encode_file(
                            relative, await asyncio.to_thread(path.read_bytes)
                        )
                        stats["bytes_sent"] += len(encoded_file)
                        stats["checkpoint_bytes"] += path.stat().st_size
                        yield encoded_file
                    end = canonical_json({
                        "v": PROTOCOL_VERSION,
                        "kind": "checkpoint.end",
                        "file_count": len(files),
                        "total_bytes": total,
                    })
                    stats["bytes_sent"] += len(end)
                    yield end
                deltas = await scheduler._handle(
                    token,
                    encode_pull_request(
                        requested_epoch,
                        after_transaction_ref=after_transaction_ref,
                    ),
                    peer_pub,
                    telemetry_channel="relay",
                    telemetry_mode="checkpoint" if include_checkpoint else "delta",
                    telemetry_stats=stats,
                    telemetry_started_at_ns=started_at_ns,
                    telemetry_started_monotonic_ns=started_monotonic_ns,
                )
                async for frame in deltas:
                    yield frame
                outcome = "success"
                error_code = ""
            except asyncio.CancelledError:
                outcome = "cancelled"
                error_code = ""
                raise
            except Exception:
                # This generator's own body -- everything from the
                # server-hello yield onward -- runs lazily, driven by
                # whatever iterates handle()'s return value at the actual
                # WS-send layer, not by _fleet_sync's try/except (which
                # only covers handle()'s own synchronous setup, already
                # complete by the time this generator is even created).
                # An exception here was silently swallowed before -- no
                # server-hello sent, connection just closes clean (1000),
                # with nothing logged anywhere. Found live 2026-08-23
                # chasing exactly that symptom.
                logger.warning("fleet relay sync stream failed", exc_info=True)
                error_code = "relay_stream_failed"
                raise
            finally:
                shutil.rmtree(root, ignore_errors=True)
                recorder = scheduler.config.telemetry_recorder
                if recorder is not None:
                    with contextlib.suppress(Exception):
                        await asyncio.to_thread(
                            recorder,
                            peer_pub,
                            channel="relay",
                            direction="serve",
                            mode="checkpoint" if include_checkpoint else "delta",
                            outcome=outcome,
                            started_at_ns=started_at_ns,
                            duration_ms=max(
                                0,
                                (time.monotonic_ns() - started_monotonic_ns)
                                // 1_000_000,
                            ),
                            error_code=error_code,
                            **stats,
                        )

        return stream()


connector_runtime = ConnectorFleetRuntime()


def _route_location(rendezvous: str) -> tuple[str, str, str]:
    parsed = urllib.parse.urlsplit(rendezvous)
    parts = parsed.path.split("/")
    token = parts[-1] if len(parts) == 3 and parts[1] == "l" else ""
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or len(token) != 32
        or any(char not in "0123456789abcdef" for char in token)
    ):
        raise FleetRelaySyncError("stored Fleet route is not an exact HTTPS link")
    return f"https://{parsed.netloc}", f"wss://{parsed.netloc}", token


async def _fetch_envelope(base: str, token: str) -> dict:
    import httpx

    async with httpx.AsyncClient(base_url=base, timeout=10.0) as client:
        response = await client.get(f"/v1/links/{token}/envelope")
    if response.status_code != 200:
        raise FleetRelaySyncError("stored Fleet route is unavailable")
    value = response.json()
    if (
        not isinstance(value, dict)
        or value.get("target_type") != "fleet:join"
        or not isinstance(value.get("root_pub"), str)
        or not isinstance(value.get("org"), str)
    ):
        raise FleetRelaySyncError("stored Fleet route envelope is not a Fleet link")
    return value


async def pull_checkpoint_once(
    credential: fleet_runtime.FleetRuntimeCredential,
    route: fleet_route.FleetRoute,
    *,
    include_checkpoint: bool = True,
    metrics: dict[str, int] | None = None,
) -> dict[str, int]:
    metrics = metrics if metrics is not None else {}
    metrics.update({
        "bytes_sent": 0,
        "bytes_received": 0,
        "mutation_frames": 0,
        "transactions": 0,
        "checkpoint_bytes": 0,
    })
    entries = tuple(fleet_roster.load_entries(org=None))
    root_pub = credential.delegation_cert.org.removeprefix("personal:")
    auth = FleetAuthenticator(
        credential.process_key,
        root_pub=root_pub,
        roster_entries=lambda: fleet_roster.load_entries(org=None),
        roster_machine_pub=credential.machine_pub,
        delegation_cert=credential.delegation_cert,
        require_delegation=True,
    )
    epoch = roster_epoch(entries, root_pub)
    http_base, ws_base, token = _route_location(route.rendezvous)
    envelope = await _fetch_envelope(http_base, token)
    channel = await ViewerChannel.connect(
        ws_base,
        token,
        root_pub=envelope["root_pub"],
        org=envelope["org"],
    )
    stage_root = Path(tempfile.mkdtemp(prefix="fleet-received-checkpoint-"))
    checkpoint = stage_root / "checkpoint"
    checkpoint.mkdir()
    try:
        private, hello = auth.build_client_hello(token)
        client_eph = _json(hello, "fleet client hello")["eph_pub"]
        after_transaction_ref = await asyncio.to_thread(
            fleet_sync_telemetry.read_acknowledged_transaction_ref,
            route.origin_machine_pub,
        )
        request = canonical_json({
            "v": PROTOCOL_VERSION,
            "op": PULL_OP,
            "roster_epoch": epoch,
            "checkpoint": include_checkpoint,
            "after_transaction_ref": after_transaction_ref,
            "hello": _json(hello, "fleet client hello"),
        })
        metrics["bytes_sent"] += len(request)
        await channel.send_message(request)
        expected_files = expected_bytes = seen_files = seen_bytes = None
        saw_hello = False
        installed_checkpoint = False
        store = SQLiteFleetSyncStore(_org_db_path("personal"))
        pending = []
        pending_identity = None
        pending_count = None
        delta_digest = hashlib.sha256()
        delta_count = 0
        saw_done = False

        async def apply_pending() -> None:
            nonlocal pending
            if not pending:
                return
            if pending_count != len(pending) or len({
                item.operation_index for item in pending
            }) != len(pending):
                raise FleetRelaySyncError(
                    "fleet relay transaction is incomplete or out of order"
                )
            await asyncio.to_thread(store.apply, pending)
            metrics["transactions"] += 1
            pending = []

        async for raw, final in channel.recv_message_stream():
            metrics["bytes_received"] += len(raw)
            if not saw_hello:
                first = _json(raw, "fleet server hello envelope")
                if first.get("kind") == "fleet.server-error":
                    raise FleetRelaySyncError(
                        f"fleet server refused: {first.get('error', 'unknown reason')}"
                    )
                if set(first) != {"v", "kind", "hello", "roster_epoch"} \
                        or first.get("v") != PROTOCOL_VERSION \
                        or first.get("kind") != "fleet.server-hello":
                    raise FleetRelaySyncError("fleet server hello envelope is malformed")
                auth.verify_server(
                    canonical_json(first["hello"]),
                    session=token,
                    client_eph=client_eph,
                    expected_machine_pub=route.origin_machine_pub,
                )
                saw_hello = True
                continue
            if raw.startswith(_MUTATION_MAGIC):
                item, operation_count = decode_authored(raw)
                identity = (
                    item.origin_incarnation,
                    item.transaction_id,
                    item.mutation.timestamp_ns,
                )
                if pending_identity is not None and identity != pending_identity:
                    await apply_pending()
                pending_identity = identity
                if pending and pending_count != operation_count:
                    raise FleetRelaySyncError(
                        "fleet relay transaction operation count changed"
                    )
                pending_count = operation_count
                pending.append(item)
                delta_digest.update(struct.pack(">Q", len(raw)))
                delta_digest.update(raw)
                delta_count += 1
                metrics["mutation_frames"] += 1
                continue
            if raw.startswith(_DONE_MAGIC):
                await apply_pending()
                (
                    remote_epoch,
                    expected_count,
                    expected_digest,
                    through_transaction_ref,
                ) = decode_done(raw)
                if delta_count != expected_count \
                        or delta_digest.hexdigest() != expected_digest:
                    raise FleetRelaySyncError("fleet relay delta digest mismatch")
                _ = remote_epoch
                metrics["acknowledged_transaction_ref"] = (
                    through_transaction_ref
                )
                saw_done = True
                break
            if raw.startswith(FILE_MAGIC):
                if expected_files is None:
                    raise FleetRelaySyncError("checkpoint file arrived before its header")
                relative, body = _decode_file(raw)
                target = checkpoint.joinpath(*PurePosixPath(relative).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as handle:
                    handle.write(body)
                    handle.flush()
                    os.fsync(handle.fileno())
                seen_files += 1
                seen_bytes += len(body)
                metrics["checkpoint_bytes"] += len(body)
                continue
            value = _json(raw, "checkpoint control")
            kind = value.get("kind")
            if kind == "checkpoint.begin":
                if not include_checkpoint:
                    raise FleetRelaySyncError("unexpected checkpoint on delta pull")
                expected = {
                    "v", "kind", "file_count", "total_bytes",
                    "source_machine_pub", "roster_epoch",
                }
                if set(value) != expected or value["source_machine_pub"] != route.origin_machine_pub:
                    raise FleetRelaySyncError("checkpoint header is malformed")
                expected_files = value["file_count"]
                expected_bytes = value["total_bytes"]
                if (
                    not isinstance(expected_files, int)
                    or not isinstance(expected_bytes, int)
                    or expected_files < 1
                    or expected_files > MAX_CHECKPOINT_FILES
                    or expected_bytes < 1
                    or expected_bytes > MAX_CHECKPOINT_BYTES
                ):
                    raise FleetRelaySyncError("checkpoint header exceeds bounds")
                seen_files = seen_bytes = 0
                continue
            if kind == "checkpoint.end":
                if (
                    expected_files is None
                    or seen_files != expected_files
                    or seen_bytes != expected_bytes
                    or value.get("file_count") != seen_files
                    or value.get("total_bytes") != seen_bytes
                ):
                    raise FleetRelaySyncError("checkpoint stream is incomplete")
                await dashboard_fleet_sync_service.install_checkpoint(
                    checkpoint,
                    source_machine_pub=route.origin_machine_pub,
                )
                store = SQLiteFleetSyncStore(_org_db_path("personal"))
                installed_checkpoint = True
                continue
            raise FleetRelaySyncError("checkpoint stream has an unknown control")
        if not saw_done:
            raise FleetRelaySyncError("fleet relay stream ended without delta summary")
        if include_checkpoint and not installed_checkpoint:
            raise FleetRelaySyncError("fleet relay stream ended without checkpoint")
    finally:
        await channel.close()

    try:
        entries = tuple(fleet_roster.load_entries(org=None))
        epoch = roster_epoch(entries, root_pub)
        await asyncio.to_thread(
            SQLiteFleetSyncStore(_org_db_path("personal")).record_peer,
            route.origin_machine_pub,
            epoch,
            online=False,
            checkpoints_received=int(installed_checkpoint),
            deltas_received=1,
            acknowledgements=1,
            success=True,
        )
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)
    return metrics


#: Distinct, greppable classification for the last pull attempt -- the
#: "money line" a diagnostic needs instead of re-deriving it from a raw
#: traceback each time. Order matters: first matching classifier wins.
def _classify_pull_failure(exc: BaseException) -> str:
    from tools.network.fleet_sync.compaction import WatermarkError
    from tools.network.fleet_sync.codec import CodecError
    from tools.network.fleet_sync.sync import AlphaError

    text = str(exc)
    if isinstance(exc, WatermarkError):
        if "hash mismatch" in text:
            return "hash_mismatch"
        if "missing live row" in text:
            return "missing_live_row"
        return "watermark_error"
    if isinstance(exc, CodecError):
        return "codec_error"
    if isinstance(exc, AlphaError):
        if "unavailable attachment bytes" in text:
            return "attachment_bytes_unavailable"
        return "alpha_error"
    if isinstance(exc, FleetRelaySyncError):
        if "locked" in text:
            return "locked"
        if "malformed" in text:
            return "malformed_hello"
        if "TTL" in text or "exceeds its TTL" in text:
            return "ttl_bound"
        return "protocol_error"
    code = getattr(exc, "code", None)
    if code == 4404:
        return "unknown_link"
    if code is not None:
        return f"relay_close_{code}"
    return type(exc).__name__


class DashboardFleetRelaySyncService:
    """Retry the machine-local origin route while this runtime is unlocked."""

    def __init__(self) -> None:
        self._credential: fleet_runtime.FleetRuntimeCredential | None = None
        self._task: asyncio.Task | None = None
        #: The single "money line" fleet_doctor reads instead of grepping
        #: logs -- distinct outcome + reason for the most recent attempt,
        #: whichever process is actually running this loop right now.
        self.last_result: dict[str, object] | None = None

    def configure(self, credential: fleet_runtime.FleetRuntimeCredential) -> None:
        self._credential = credential
        if self._task is not None and not self._task.done():
            self._task.cancel()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._task = loop.create_task(self._run(credential), name="fleet-relay-sync")

    async def _run(self, credential: fleet_runtime.FleetRuntimeCredential) -> None:
        import time

        delay = 0.5
        while self._credential is credential:
            route = None
            include_checkpoint = False
            started_at_ns = time.time_ns()
            started_monotonic_ns = time.monotonic_ns()
            metrics: dict[str, int] = {}
            try:
                route = await asyncio.to_thread(fleet_route.load, org="machine")
                if route is None:
                    return
                include_checkpoint = not await asyncio.to_thread(
                    _has_checkpoint,
                    route.origin_machine_pub,
                    credential.delegation_cert.org.removeprefix("personal:"),
                )
                await pull_checkpoint_once(
                    credential,
                    route,
                    include_checkpoint=include_checkpoint,
                    metrics=metrics,
                )
                duration_ms = max(
                    0, (time.monotonic_ns() - started_monotonic_ns) // 1_000_000
                )
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(
                        fleet_sync_telemetry.record_iteration,
                        route.origin_machine_pub,
                        channel="relay",
                        direction="pull",
                        mode="checkpoint" if include_checkpoint else "delta",
                        outcome="success",
                        started_at_ns=started_at_ns,
                        duration_ms=duration_ms,
                        **metrics,
                    )
                self.last_result = {"outcome": "success", "reason": None, "at": time.time()}
                # The happy path was completely silent before this line -- a
                # working delta pull and "nothing has attempted a pull in a
                # while" looked identical in the logs, which cost a real
                # multi-hour debugging session on 2026-08-24 chasing a delta-
                # propagation bug that didn't exist. One line, not a WARNING
                # (failures already log loudly below) -- just proof of life.
                logger.info(
                    "fleet relay sync: pull succeeded (%s)",
                    "checkpoint" if include_checkpoint else "delta",
                )
                delay = 0.5
                await asyncio.sleep(10.0)
            except asyncio.CancelledError:
                if route is not None:
                    with contextlib.suppress(Exception):
                        await asyncio.to_thread(
                            fleet_sync_telemetry.record_iteration,
                            route.origin_machine_pub,
                            channel="relay",
                            direction="pull",
                            mode="checkpoint" if include_checkpoint else "delta",
                            outcome="cancelled",
                            started_at_ns=started_at_ns,
                            duration_ms=max(
                                0,
                                (time.monotonic_ns() - started_monotonic_ns)
                                // 1_000_000,
                            ),
                            **metrics,
                        )
                raise
            except Exception as exc:
                reason = _classify_pull_failure(exc)
                if route is not None:
                    with contextlib.suppress(Exception):
                        await asyncio.to_thread(
                            fleet_sync_telemetry.record_iteration,
                            route.origin_machine_pub,
                            channel="relay",
                            direction="pull",
                            mode="checkpoint" if include_checkpoint else "delta",
                            outcome="failed",
                            started_at_ns=started_at_ns,
                            duration_ms=max(
                                0,
                                (time.monotonic_ns() - started_monotonic_ns)
                                // 1_000_000,
                            ),
                            error_code=reason,
                            **metrics,
                        )
                self.last_result = {
                    "outcome": "failed",
                    "reason": reason,
                    "detail": str(exc)[:300],
                    "at": time.time(),
                }
                logger.warning("Fleet relay checkpoint pull failed", exc_info=True)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)


dashboard_relay_sync_service = DashboardFleetRelaySyncService()


def _has_checkpoint(machine_pub: str, root_pub: str) -> bool:
    path = _org_db_path("personal")
    try:
        entries = tuple(fleet_roster.load_entries(org=None))
        epoch = roster_epoch(entries, root_pub)
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            row = conn.execute(
                "SELECT 1 FROM fleet_sync_peer_state WHERE "
                "machine_public_key=? AND roster_epoch=? "
                "AND checkpoints_received>0 LIMIT 1",
                (machine_pub, epoch),
            ).fetchone()
        return row is not None
    except (OSError, sqlite3.Error, ValueError):
        return False


_ORG_UNSET = object()


def publish_connector_runtime(payload: object, *, org=_ORG_UNSET) -> None:
    """Hand the serving subprocess the same short-lived process credential.

    ``org=None`` is a meaningful, valid target -- the scopeless/personal
    serving connector -- not "unspecified". Only an omitted *org* falls back
    to ``shell_default_org()``; ``org or shell_default_org()`` would silently
    treat an explicit ``org=None`` the same as "not passed", which is
    exactly the bug that misrouted this call to whatever org happened to be
    cosmetically default instead of the scope the caller actually meant.
    """
    from tools.dashboard import link_serving_supervisor
    from tools.graph.schemas.dashboard_shell import shell_default_org

    target_org = shell_default_org() if org is _ORG_UNSET else org
    reply = link_serving_supervisor.control(target_org, CONTROL_OP, payload)
    if not isinstance(reply, dict) or reply.get("ok") is not True:
        raise FleetRelaySyncError(
            (reply or {}).get("error", "serving connector refused Fleet runtime")
            if isinstance(reply, dict)
            else "serving connector returned no Fleet runtime result"
        )
