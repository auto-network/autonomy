"""Roster-driven personal-database synchronization inside the Dashboard.

The service is deliberately in-process: one scheduler owns the direct
listener and outbound peer sessions, while each SQLite operation uses a short
fresh connection on a worker thread. No file watcher, daemon, or long-lived
database transaction participates.

This first production slice replays the retained authored journal on every
successful pull, reading and releasing one complete transaction at a time.
Remote application is idempotent, so reconnect is safe even
when a prior connection died after committing a transaction but before its
summary arrived. Exact peer ACK floors and journal retirement are a later
contract; replay avoids inventing an unsafe scalar cursor in the meantime.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

from tools.network.fleet_roster import RosterEntry, resolve
from tools.network.fleet_sync_channel import (
    FleetAuthenticator,
    FleetDirectServer,
    fleet_direct_connect,
)
from tools.network.fleet_sync_connection import FleetSyncConnection
from tools.network.fleet_sync.catalog import (
    AuthoredMutation,
    MAX_TRANSACTION_OPERATIONS,
    MutationCatalog,
    WatermarkError,
    attach_active_production_catalog,
)
from tools.network.fleet_sync.codec import (
    decode_mutation_frame,
    encode_mutation_frame,
)
from tools.network.idkit import KeyPair, canonical_json
from tools.network.idkit import DelegationCert
from tools.network.relaykit.channel import MAX_MESSAGE_SIZE
from tools.network.relaykit.direct import new_session_id

logger = logging.getLogger(__name__)

FLEET_SYNC_PROTOCOL_VERSION = 1
_REQUEST_FIELDS = frozenset({"v", "op", "roster_epoch"})
_DONE_FIELDS = frozenset(
    {"v", "kind", "roster_epoch", "message_count", "digest"}
)
_MUTATION_MAGIC = b"FST1"
_DONE_MAGIC = b"FSD1"
_HEADER_LIMIT = 4096


class FleetSyncProtocolError(ValueError):
    """A peer sent malformed, inconsistent, or unsupported sync data."""


@dataclass(frozen=True)
class FleetSyncRuntimeConfig:
    """Runtime material the unlocked fleet-identity boundary must supply.

    ``peer_addresses`` maps active machine public keys to direct RelayKit
    WebSocket candidates. The peer never supplies a local database path.
    """

    machine_key: KeyPair
    personal_root_pub: str
    roster_entries: Callable[[], Iterable[RosterEntry]]
    peer_addresses: Callable[[], Mapping[str, Sequence[str]]]
    personal_db_path: Path
    roster_machine_pub: str | None = None
    delegation_cert: DelegationCert | None = None
    require_delegation: bool = False
    listen_host: str = "127.0.0.1"
    listen_port: int = 0
    poll_interval: float = 1.0
    connect_timeout: float = 3.0
    min_backoff: float = 0.25
    max_backoff: float = 5.0


def roster_epoch(entries: Iterable[RosterEntry], root_pub: str) -> str:
    active = sorted(resolve(entries, anchor_root_pub=root_pub))
    return hashlib.sha256(
        b"autonomy.network.fleet-sync.roster-epoch.v1\n"
        + canonical_json(active)
    ).hexdigest()


def _json_object(raw: bytes, fields: frozenset[str], what: str) -> dict:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise FleetSyncProtocolError(f"{what} is not valid JSON") from exc
    if not isinstance(value, dict) or set(value) != fields:
        raise FleetSyncProtocolError(
            f"{what} must carry exactly {sorted(fields)}"
        )
    if value["v"] != FLEET_SYNC_PROTOCOL_VERSION:
        raise FleetSyncProtocolError(
            f"unsupported {what} version: {value['v']!r}"
        )
    return value


def encode_pull_request(epoch: str) -> bytes:
    return canonical_json(
        {"v": FLEET_SYNC_PROTOCOL_VERSION, "op": "pull", "roster_epoch": epoch}
    )


def decode_pull_request(raw: bytes) -> str:
    value = _json_object(raw, _REQUEST_FIELDS, "fleet sync request")
    if value["op"] != "pull":
        raise FleetSyncProtocolError("unsupported fleet sync operation")
    epoch = value["roster_epoch"]
    if (
        not isinstance(epoch, str)
        or len(epoch) != 64
        or any(ch not in "0123456789abcdef" for ch in epoch)
    ):
        raise FleetSyncProtocolError("fleet sync roster_epoch is malformed")
    return epoch


def encode_authored(item: AuthoredMutation, *, transaction_operations: int) -> bytes:
    frame = encode_mutation_frame(item.mutation)
    header = canonical_json(
        {
            "origin": item.origin_incarnation,
            "transaction": item.transaction_id,
            "operation": item.operation_index,
            "transaction_operations": transaction_operations,
        }
    )
    message = _MUTATION_MAGIC + struct.pack(">I", len(header)) + header + frame
    if len(header) > _HEADER_LIMIT or len(message) > MAX_MESSAGE_SIZE:
        raise FleetSyncProtocolError(
            "authored mutation exceeds the fleet channel message bound"
        )
    return message


def decode_authored(raw: bytes) -> tuple[AuthoredMutation, int]:
    if len(raw) < 8 or raw[:4] != _MUTATION_MAGIC:
        raise FleetSyncProtocolError("fleet mutation message has wrong magic")
    header_len = struct.unpack(">I", raw[4:8])[0]
    if not 1 <= header_len <= _HEADER_LIMIT or 8 + header_len >= len(raw):
        raise FleetSyncProtocolError("fleet mutation header length is invalid")
    try:
        header = json.loads(raw[8:8 + header_len])
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FleetSyncProtocolError("fleet mutation header is not valid JSON") from exc
    if not isinstance(header, dict) or set(header) != {
        "origin", "transaction", "operation", "transaction_operations"
    }:
        raise FleetSyncProtocolError("fleet mutation header has wrong fields")
    origin = header["origin"]
    transaction = header["transaction"]
    operation = header["operation"]
    operation_count = header["transaction_operations"]
    if (
        not isinstance(origin, str)
        or len(origin) != 64
        or any(ch not in "0123456789abcdef" for ch in origin)
    ):
        raise FleetSyncProtocolError("fleet mutation origin is malformed")
    if not isinstance(transaction, str) or not transaction:
        raise FleetSyncProtocolError("fleet mutation transaction is malformed")
    if not isinstance(operation, int) or isinstance(operation, bool) or operation < 0:
        raise FleetSyncProtocolError("fleet mutation operation is malformed")
    if (
        not isinstance(operation_count, int)
        or isinstance(operation_count, bool)
        or not 1 <= operation_count <= MAX_TRANSACTION_OPERATIONS
    ):
        raise FleetSyncProtocolError(
            "fleet mutation transaction operation count is malformed"
        )
    try:
        mutation = decode_mutation_frame(raw[8 + header_len:])
    except Exception as exc:
        raise FleetSyncProtocolError("fleet mutation frame is malformed") from exc
    return AuthoredMutation(origin, transaction, operation, mutation), operation_count


def encode_done(*, epoch: str, count: int, digest: str) -> bytes:
    return _DONE_MAGIC + canonical_json(
        {
            "v": FLEET_SYNC_PROTOCOL_VERSION,
            "kind": "done",
            "roster_epoch": epoch,
            "message_count": count,
            "digest": digest,
        }
    )


def decode_done(raw: bytes) -> tuple[str, int, str]:
    if not raw.startswith(_DONE_MAGIC):
        raise FleetSyncProtocolError("fleet sync summary has wrong magic")
    value = _json_object(raw[len(_DONE_MAGIC):], _DONE_FIELDS, "fleet sync summary")
    if value["kind"] != "done":
        raise FleetSyncProtocolError("fleet sync summary has wrong kind")
    epoch = value["roster_epoch"]
    digest = value["digest"]
    count = value["message_count"]
    for name, candidate in (("roster_epoch", epoch), ("digest", digest)):
        if (
            not isinstance(candidate, str)
            or len(candidate) != 64
            or any(ch not in "0123456789abcdef" for ch in candidate)
        ):
            raise FleetSyncProtocolError(f"fleet sync summary {name} is malformed")
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise FleetSyncProtocolError("fleet sync summary message_count is malformed")
    return epoch, count, digest


def _digest_add(digest, message: bytes) -> None:
    digest.update(len(message).to_bytes(8, "big"))
    digest.update(message)


class SQLiteFleetSyncStore:
    """Short-connection access to one fixed activated personal database."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def _open(self) -> tuple[FleetSyncConnection, MutationCatalog]:
        import sqlite3

        conn = sqlite3.connect(self.path, factory=FleetSyncConnection)
        catalog = attach_active_production_catalog(conn)
        if catalog is None:
            conn.close()
            raise WatermarkError("personal database fleet writers are not active")
        return conn, catalog

    def next_transaction(
        self, after: tuple[int, str, str] | None
    ) -> tuple[tuple[int, str, str], list[AuthoredMutation]] | None:
        conn, catalog = self._open()
        try:
            return catalog.next_journal_transaction(after)
        finally:
            conn.close()

    def apply(self, items: list[AuthoredMutation]) -> tuple[int, int]:
        conn, catalog = self._open()
        try:
            return catalog.apply_remote_batch(items)
        finally:
            conn.close()

    def record_peer(
        self,
        machine_pub: str,
        epoch: str,
        *,
        online: bool,
        bytes_sent: int = 0,
        bytes_received: int = 0,
        checkpoints_received: int = 0,
        deltas_received: int = 0,
        transactions_applied: int = 0,
        acknowledgements: int = 0,
        retries: int = 0,
        peer_watermark: int | None = None,
        error: str | None = None,
        success: bool = False,
    ) -> None:
        conn, _catalog = self._open()
        now = time.time_ns()
        try:
            conn.execute(
                "INSERT INTO fleet_sync_peer_state("
                "machine_public_key,roster_epoch,online,last_success_ns,"
                "peer_watermark,bytes_sent,bytes_received,checkpoints_received,"
                "deltas_received,transactions_applied,acknowledgements,retries,"
                "last_error_code,updated_at_ns) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(machine_public_key,roster_epoch) DO UPDATE SET "
                "online=excluded.online,"
                "last_success_ns=CASE WHEN ? THEN excluded.updated_at_ns "
                "ELSE fleet_sync_peer_state.last_success_ns END,"
                "peer_watermark=CASE "
                "WHEN excluded.peer_watermark IS NULL THEN "
                "fleet_sync_peer_state.peer_watermark "
                "WHEN fleet_sync_peer_state.peer_watermark IS NULL OR "
                "excluded.peer_watermark>fleet_sync_peer_state.peer_watermark "
                "THEN excluded.peer_watermark "
                "ELSE fleet_sync_peer_state.peer_watermark END,"
                "bytes_sent=fleet_sync_peer_state.bytes_sent+excluded.bytes_sent,"
                "bytes_received=fleet_sync_peer_state.bytes_received+excluded.bytes_received,"
                "checkpoints_received=fleet_sync_peer_state.checkpoints_received+"
                "excluded.checkpoints_received,"
                "deltas_received=fleet_sync_peer_state.deltas_received+"
                "excluded.deltas_received,"
                "transactions_applied=fleet_sync_peer_state.transactions_applied+"
                "excluded.transactions_applied,"
                "acknowledgements=fleet_sync_peer_state.acknowledgements+"
                "excluded.acknowledgements,"
                "retries=fleet_sync_peer_state.retries+excluded.retries,"
                "last_error_code=excluded.last_error_code,"
                "updated_at_ns=excluded.updated_at_ns",
                (
                    machine_pub, epoch, int(online), now if success else None,
                    peer_watermark, bytes_sent, bytes_received,
                    checkpoints_received, deltas_received, transactions_applied,
                    acknowledgements, retries, error, now, int(success),
                ),
            )
            conn.commit()
        finally:
            conn.close()


def _transaction_identity(item: AuthoredMutation) -> tuple[str, str, int]:
    return (
        item.origin_incarnation,
        item.transaction_id,
        item.mutation.timestamp_ns,
    )


class FleetSyncScheduler:
    """One direct listener plus bounded, roster-filtered outbound pulls."""

    def __init__(self, config: FleetSyncRuntimeConfig):
        self.config = config
        self._roster_snapshot: tuple[RosterEntry, ...] = ()
        self.authenticator = FleetAuthenticator(
            config.machine_key,
            root_pub=config.personal_root_pub,
            roster_entries=lambda: self._roster_snapshot,
            roster_machine_pub=config.roster_machine_pub,
            delegation_cert=config.delegation_cert,
            require_delegation=config.require_delegation,
        )
        self.store = SQLiteFleetSyncStore(config.personal_db_path)
        self.server = FleetDirectServer(
            self.authenticator,
            self._handle,
            host=config.listen_host,
            port=config.listen_port,
        )
        self._task: asyncio.Task | None = None
        self._roster_task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._failures: dict[str, int] = {}
        self._next_attempt: dict[str, float] = {}

    @property
    def port(self) -> int:
        return self.server.port

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        self._roster_snapshot = await asyncio.to_thread(
            lambda: tuple(self.config.roster_entries())
        )
        self.authenticator.authorize(self.authenticator.machine_pub)
        await self.server.start()
        self._stopping.clear()
        self._roster_task = asyncio.create_task(
            self._refresh_roster(), name="fleet-sync-roster"
        )
        self._task = asyncio.create_task(self._run(), name="fleet-sync-scheduler")

    async def stop(self) -> None:
        self._stopping.set()
        task = self._task
        roster_task = self._roster_task
        self._task = None
        self._roster_task = None
        tasks = [candidate for candidate in (task, roster_task) if candidate]
        for candidate in tasks:
            candidate.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.server.stop()

    def _current_epoch(self) -> str:
        return roster_epoch(
            self._roster_snapshot, self.config.personal_root_pub
        )

    async def _refresh_roster(self) -> None:
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self.config.poll_interval
                )
                continue
            except asyncio.TimeoutError:
                pass
            try:
                snapshot = await asyncio.to_thread(
                    lambda: tuple(self.config.roster_entries())
                )
                # Resolve before publishing. A broken provider never replaces
                # the last verified authorization snapshot.
                resolve(snapshot, anchor_root_pub=self.config.personal_root_pub)
                self._roster_snapshot = snapshot
            except Exception:
                logger.warning("fleet roster refresh failed", exc_info=True)

    async def _handle(
        self, _token: str, message: bytes, peer_pub: str
    ):
        decode_pull_request(message)
        epoch = self._current_epoch()

        async def response():
            digest = hashlib.sha256()
            count = 0
            cursor: tuple[int, str, str] | None = None
            while True:
                self.authenticator.authorize(peer_pub)
                page = await asyncio.to_thread(
                    self.store.next_transaction, cursor
                )
                if page is None:
                    break
                cursor, items = page
                operation_count = len(items)
                for item in items:
                    self.authenticator.authorize(peer_pub)
                    encoded = encode_authored(
                        item, transaction_operations=operation_count
                    )
                    _digest_add(digest, encoded)
                    count += 1
                    yield encoded
            self.authenticator.authorize(peer_pub)
            yield encode_done(epoch=epoch, count=count, digest=digest.hexdigest())

        return response()

    async def _run(self) -> None:
        while not self._stopping.is_set():
            entries = self._roster_snapshot
            active = resolve(entries, anchor_root_pub=self.config.personal_root_pub)
            addresses = self.config.peer_addresses()
            now = asyncio.get_running_loop().time()
            peers = [
                (machine_pub, tuple(addresses.get(machine_pub, ())))
                for machine_pub in sorted(active)
                if machine_pub != self.authenticator.machine_pub
                and addresses.get(machine_pub)
                and now >= self._next_attempt.get(machine_pub, 0.0)
            ]
            if peers:
                await asyncio.gather(
                    *(self._sync_peer(machine_pub, candidates)
                      for machine_pub, candidates in peers),
                    return_exceptions=True,
                )
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self.config.poll_interval
                )
            except asyncio.TimeoutError:
                pass

    async def _sync_peer(self, machine_pub: str, addresses: Sequence[str]) -> None:
        epoch = self._current_epoch()
        channel = None
        sent = 0
        received = 0
        peer_watermark: int | None = None
        try:
            last_error: Exception | None = None
            for address in addresses:
                try:
                    channel = await fleet_direct_connect(
                        address,
                        authenticator=self.authenticator,
                        expected_machine_pub=machine_pub,
                        session=new_session_id(),
                        timeout=self.config.connect_timeout,
                    )
                    break
                except Exception as exc:
                    last_error = exc
            if channel is None:
                assert last_error is not None
                raise last_error

            await asyncio.to_thread(
                self.store.record_peer, machine_pub, epoch, online=True
            )
            request = encode_pull_request(epoch)
            sent += len(request)
            await channel.send_message(request)

            digest = hashlib.sha256()
            message_count = 0
            pending: list[AuthoredMutation] = []
            pending_identity = None
            pending_count: int | None = None
            saw_done = False

            async def apply_pending(items: list[AuthoredMutation]) -> None:
                nonlocal peer_watermark
                won, _ignored = await asyncio.to_thread(self.store.apply, items)
                peer_watermark = max(
                    item.mutation.timestamp_ns for item in items
                )
                if won:
                    await asyncio.to_thread(
                        self.store.record_peer,
                        machine_pub,
                        epoch,
                        online=True,
                        transactions_applied=1,
                        peer_watermark=peer_watermark,
                    )

            def validate_pending() -> None:
                if not pending:
                    return
                if pending_count != len(pending) or len({
                    item.operation_index for item in pending
                }) != len(pending):
                    raise FleetSyncProtocolError(
                        "fleet transaction is incomplete or out of order"
                    )

            async for message, stream_final in channel.recv_message_stream():
                # A kick that lands after the hello revokes this live session
                # before another application message is accepted.
                self.authenticator.authorize(machine_pub)
                received += len(message)
                if message.startswith(_DONE_MAGIC):
                    remote_epoch, expected_count, expected_digest = decode_done(message)
                    if not stream_final:
                        raise FleetSyncProtocolError("fleet summary is not final")
                    if pending:
                        validate_pending()
                        await apply_pending(pending)
                        pending = []
                    if message_count != expected_count:
                        raise FleetSyncProtocolError("fleet message count mismatch")
                    if digest.hexdigest() != expected_digest:
                        raise FleetSyncProtocolError("fleet stream digest mismatch")
                    # A differing epoch is observable state, not authority:
                    # each accepted key already passed the receiver's roster.
                    _ = remote_epoch
                    saw_done = True
                    break
                if stream_final:
                    raise FleetSyncProtocolError("fleet mutation ended the stream")
                item, operation_count = decode_authored(message)
                identity = _transaction_identity(item)
                if pending_identity is not None and identity != pending_identity:
                    validate_pending()
                    await apply_pending(pending)
                    pending = []
                pending_identity = identity
                if pending and pending_count != operation_count:
                    raise FleetSyncProtocolError(
                        "fleet transaction operation count changed"
                    )
                pending_count = operation_count
                pending.append(item)
                _digest_add(digest, message)
                message_count += 1
            if not saw_done:
                raise FleetSyncProtocolError("fleet stream ended without summary")

            await asyncio.to_thread(
                self.store.record_peer,
                machine_pub,
                epoch,
                online=False,
                bytes_sent=sent,
                bytes_received=received,
                deltas_received=1,
                acknowledgements=1,
                peer_watermark=peer_watermark,
                success=True,
            )
            self._failures.pop(machine_pub, None)
            self._next_attempt.pop(machine_pub, None)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(
                    self.store.record_peer,
                    machine_pub,
                    epoch,
                    online=False,
                    bytes_sent=sent,
                    bytes_received=received,
                )
            raise
        except Exception as exc:
            failures = self._failures.get(machine_pub, 0) + 1
            self._failures[machine_pub] = failures
            delay = min(
                self.config.max_backoff,
                self.config.min_backoff * (2 ** min(failures - 1, 16)),
            )
            self._next_attempt[machine_pub] = (
                asyncio.get_running_loop().time() + delay
            )
            with contextlib.suppress(Exception):
                await asyncio.to_thread(
                    self.store.record_peer,
                    machine_pub,
                    epoch,
                    online=False,
                    bytes_sent=sent,
                    bytes_received=received,
                    retries=1,
                    error=type(exc).__name__,
                )
            logger.warning(
                "fleet sync peer %s failed (%s)",
                machine_pub[:12],
                type(exc).__name__,
            )
        finally:
            if channel is not None:
                await channel.close()


class DashboardFleetSyncService:
    """Lifecycle-owned scheduler that remains healthy before unlock/config."""

    def __init__(self):
        self._config: FleetSyncRuntimeConfig | None = None
        self._scheduler: FleetSyncScheduler | None = None
        self._task: asyncio.Task | None = None
        self._changed = asyncio.Event()
        self._stopping = False
        self._transition = asyncio.Lock()

    @property
    def scheduler(self) -> FleetSyncScheduler | None:
        return self._scheduler

    def configure(self, config: FleetSyncRuntimeConfig | None) -> None:
        self._config = config
        self._changed.set()

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        self._task = asyncio.create_task(
            self._run(), name="dashboard-fleet-sync-service"
        )

    async def stop(self) -> None:
        self._stopping = True
        self._changed.set()
        task = self._task
        self._task = None
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def install_checkpoint(
        self, checkpoint_directory: Path, *, source_machine_pub: str
    ):
        """Pause this runtime, publish a received base, then resume deltas.

        Closing the scheduler and pooled GraphDB handle is the cooperative
        quiescence path. Any other live production store handle causes the
        process-wide gate to refuse publication instead of swapping beneath
        an unknown writer.
        """
        config = self._config
        if config is None:
            raise RuntimeError("fleet sync runtime is not configured")
        from tools.graph.db import GraphDB
        from tools.network.fleet_checkpoint_handoff import (
            install_quiesced_checkpoint,
        )
        from tools.network.fleet_sync_connection import (
            acquire_database_quiescence,
        )

        async with self._transition:
            if self._scheduler is not None:
                await self._scheduler.stop()
                self._scheduler = None
            token = None
            installed = None
            epoch = None
            try:
                await asyncio.to_thread(
                    GraphDB.close_pooled_path, config.personal_db_path
                )
                token = await asyncio.to_thread(
                    acquire_database_quiescence, config.personal_db_path
                )
                entries = await asyncio.to_thread(
                    lambda: tuple(config.roster_entries())
                )
                active = tuple(sorted(resolve(
                    entries, anchor_root_pub=config.personal_root_pub
                )))
                if (
                    source_machine_pub not in active
                    or source_machine_pub == (
                        config.roster_machine_pub or config.machine_key.public_hex
                    )
                ):
                    raise RuntimeError(
                        "checkpoint source is not an active remote fleet machine"
                    )
                epoch = roster_epoch(entries, config.personal_root_pub)
                installed = await asyncio.to_thread(
                    install_quiesced_checkpoint,
                    checkpoint_directory,
                    config.personal_db_path,
                    quiescence=token,
                    target_origin_incarnation=(
                        config.roster_machine_pub or config.machine_key.public_hex
                    ),
                    expected_roster_epoch=epoch,
                    expected_active_roster=active,
                    source_machine_pub=source_machine_pub,
                )
            finally:
                if token is not None:
                    token.release()
                if not self._stopping and self._config is config:
                    self._scheduler = FleetSyncScheduler(config)
                    await self._scheduler.start()
            assert installed is not None and epoch is not None
            return installed

    async def _run(self) -> None:
        current: FleetSyncRuntimeConfig | None = None
        while not self._stopping:
            self._changed.clear()
            desired = self._config
            if desired is not current:
                async with self._transition:
                    if self._scheduler is not None:
                        await self._scheduler.stop()
                        self._scheduler = None
                    current = desired
                    if desired is not None:
                        self._scheduler = FleetSyncScheduler(desired)
                        await self._scheduler.start()
            await self._changed.wait()
        async with self._transition:
            if self._scheduler is not None:
                await self._scheduler.stop()
                self._scheduler = None


dashboard_fleet_sync_service = DashboardFleetSyncService()


def configure_dashboard_fleet_sync(config: FleetSyncRuntimeConfig | None) -> None:
    """Install/clear unlocked runtime material without persisting key bytes."""
    dashboard_fleet_sync_service.configure(config)
