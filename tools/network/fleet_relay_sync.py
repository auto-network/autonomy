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
    decode_breadcrumb,
    encode_pull_request,
    encode_breadcrumb,
    FleetSyncProtocolError,
    FleetSyncRuntimeConfig,
    FleetSyncScheduler,
    MAX_RESUME_BREADCRUMBS,
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
#: A direct-path success younger than this makes a relay pull redundant.
DIRECT_FRESHNESS_WINDOW_S = 30.0
BLOB_OP = "fleet.sync.blob"
CONTROL_OP = "fleet-runtime"
FILE_MAGIC = b"FSB1"
MAX_CHECKPOINT_FILES = 4096
MAX_CHECKPOINT_BYTES = 4 * 1024 * 1024 * 1024
_REQUEST_FIELDS = {
    "v", "op", "roster_epoch", "checkpoint", "compat", "resume", "hello",
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


def _keycache_dir() -> Path:
    """The ramfs mount that holds warm, memory-only node secrets.

    Same mount the vault hot-reload and the delegate key use. The
    ``AUTONOMY_KEYCACHE_MOUNT`` override keeps the guard testable headlessly,
    exactly as ``unlock_routes`` resolves it.
    """
    override = os.environ.get("AUTONOMY_KEYCACHE_MOUNT")
    if override:
        return Path(override)
    from agents.secret_ramfs import KEYCACHE_MOUNT

    return Path(KEYCACHE_MOUNT)


class FleetRuntimeWarmCache:
    """A ramfs home for the connector's Fleet runtime credential (auto-ixwr3).

    The credential is minted in the browser from the personal root at unlock,
    pushed once, and held only in this process's memory. So a connector that
    restarts — a crash, or the watchdog respawning it after its dashboard died —
    came back with ``scheduler is None`` and refused every pull ("serving
    machine is locked for Fleet sync") until a human unlocked again.

    The agent delegate signing key already solved this exact problem (auto-a1pub):
    the warm secret is handed to the next process through a ramfs cache so it
    comes back armed with nobody present. This is the same treatment for the
    same class of secret. ``store`` re-checks the mount is ramfs on every write
    (memory, never swappable) and writes 0600, matching the delegate cache; a
    reboot clears ramfs, so a reboot still fails closed to a human unlock.

    Keyed by the connector's registry ``org_uuid`` so only the connector that
    was armed re-arms itself: every other org's connector reads an absent file
    and stays exactly as it was.
    """

    def __init__(
        self,
        org_uuid: str,
        *,
        directory: "Path | None" = None,
        name_prefix: str = "fleet-connector-runtime",
    ):
        # ``name_prefix`` selects which credential this cache holds. It defaults
        # to the serving connector's file so that caller is unchanged; the
        # Dashboard passes ``fleet-dashboard-runtime`` for its OWN copy
        # (auto-5er0n). The two files coexist for one ``org_uuid`` and neither
        # reads the other: a non-serving machine runs no connector, so the
        # connector file never exists there and the Dashboard must not depend on
        # it.
        self._dir = Path(directory) if directory is not None else _keycache_dir()
        self._path = self._dir / f"{name_prefix}.{org_uuid}.json"

    def store(self, payload: object) -> None:
        from tools.network.storagekit.memory_cache import assert_memory_backed

        assert_memory_backed(self._dir)  # ramfs only — refuses tmpfs/disk
        data = json.dumps(payload).encode("utf-8")
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(self._path, flags, 0o600)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
        finally:
            os.close(fd)

    def load(self) -> "dict | None":
        from tools.network.storagekit.memory_cache import assert_memory_backed

        try:
            raw = self._path.read_bytes()
        except FileNotFoundError:
            return None
        assert_memory_backed(self._dir)
        try:
            payload = json.loads(raw)
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None

    def clear(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self._path)


class ConnectorFleetRuntime:
    """Short-lived Fleet server credential held only by the connector."""

    def __init__(self) -> None:
        self.scheduler: FleetSyncScheduler | None = None
        #: Verified enrolled machine signer from the warm reachability
        #: credential.  It is memory-only and absent on a cold/legacy
        #: connector; RelayKit hello v2 must never be attempted without it.
        self.machine_key: KeyPair | None = None
        #: Per-org serving key for the tunnel hello (auto-e2ufw); when present
        #: the serving connector presents this unlinkable key instead of the
        #: fleet machine_key. None keeps the transitional fleet-key behavior.
        self.serving_machine_key: KeyPair | None = None
        #: Set at connector startup (main) so a fresh process re-arms itself
        #: from the warm ramfs cache and a successful configure() re-warms it.
        #: None on any node without a ramfs keycache — arming still works, it
        #: just does not survive a restart there.
        self._warm_cache: "FleetRuntimeWarmCache | None" = None
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
        self.machine_key = credential.machine_key
        self.serving_machine_key = credential.serving_machine_key
        # A fresh credential means this process is no longer refusing — start a
        # new refusal tally so the flag's "since" reflects THIS lock, not one
        # cleared hours ago.
        self.locked_refusals = 0
        self.first_locked_refusal_at = None
        # Hand the warm credential to the NEXT connector process through ramfs,
        # so a crash or watchdog respawn comes back armed with nobody present.
        # A cache write failure (e.g. no ramfs on this node) must not fail the
        # arm — the process is armed in memory regardless; it just will not
        # survive a restart here.
        if self._warm_cache is not None:
            try:
                self._warm_cache.store(payload)
            except Exception:
                logger.warning(
                    "fleet runtime warm-cache write failed; connector is armed "
                    "but will not survive a restart", exc_info=True)
        return {"ok": True, "machine_id": credential.machine_id}

    def attach_warm_cache(self, cache: "FleetRuntimeWarmCache | None") -> None:
        """Bind a ramfs warm cache so configure() persists the credential and
        the connector can re-arm from it at startup."""
        self._warm_cache = cache

    def rearm_from_cache(self) -> bool:
        """Re-arm this connector from the warm ramfs cache at startup.

        Returns True iff a live cached credential re-armed it. An expired or
        de-rostered payload no longer verifies in configure(); it will never
        become valid, so drop it (a fresh unlock re-mints) rather than retry it
        every restart. Staying locked is the correct fail-closed posture.
        """
        cache = self._warm_cache
        if cache is None:
            return False
        try:
            payload = cache.load()
        except Exception:
            logger.warning("fleet runtime warm-cache read failed", exc_info=True)
            return False
        if payload is None:
            return False
        try:
            self.configure(payload)
        except Exception:
            logger.warning(
                "fleet runtime warm-cache re-arm failed; clearing the stale "
                "credential (a fresh unlock will re-mint)", exc_info=True)
            with contextlib.suppress(Exception):
                cache.clear()
            return False
        logger.warning(
            "fleet runtime re-armed from the warm cache — serving without a "
            "human unlock after a connector restart")
        return True

    async def handle(self, token: str, message: dict):
        scheduler = self.scheduler
        if scheduler is None:
            self.locked_refusals += 1
            if self.first_locked_refusal_at is None:
                self.first_locked_refusal_at = time.time()
            raise FleetRelaySyncError("serving machine is locked for Fleet sync")
        if message.get("op") == BLOB_OP:
            return await self._handle_blob(token, message)
        if set(message) != _REQUEST_FIELDS or message.get("v") != PROTOCOL_VERSION \
                or message.get("op") != PULL_OP:
            raise FleetRelaySyncError("fleet sync pull has unknown fields")
        requested_epoch = message.get("roster_epoch")
        if not isinstance(requested_epoch, str) or len(requested_epoch) != 64:
            raise FleetRelaySyncError("fleet sync pull has no roster epoch")
        include_checkpoint = message.get("checkpoint")
        if not isinstance(include_checkpoint, bool):
            raise FleetRelaySyncError("fleet sync pull checkpoint flag must be bool")
        raw_resume = message.get("resume")
        if not isinstance(raw_resume, list) or len(raw_resume) > MAX_RESUME_BREADCRUMBS:
            raise FleetRelaySyncError("fleet sync pull resume trail is malformed")
        try:
            resume_trail = tuple(
                decode_breadcrumb(entry, "fleet sync resume breadcrumb")
                for entry in raw_resume
            )
        except FleetSyncProtocolError as exc:
            raise FleetRelaySyncError(str(exc)) from exc
        peer_digest = message.get("compat")
        if (
            not isinstance(peer_digest, str)
            or len(peer_digest) != 64
            or any(ch not in "0123456789abcdef" for ch in peer_digest)
        ):
            raise FleetRelaySyncError("fleet sync pull compat digest is malformed")
        local_digest = await asyncio.to_thread(
            scheduler.store.compatibility_digest
        )
        if peer_digest != local_digest:
            # Mixed-schema fleet: the puller has not applied this machine's
            # migration yet (or vice versa). Refusing before the checkpoint
            # is built pauses BOTH transports until the schemas reconverge.
            raise FleetRelaySyncError("fleet sync schema mismatch")
        hello = canonical_json(message.get("hello"))
        peer_pub, _private, server_hello, _transcript = (
            scheduler.authenticator.accept_client(hello, session=token)
        )
        current_epoch = scheduler._current_epoch()
        # Continuity decides the transfer, not the request alone: a
        # resolvable breadcrumb trail proves the peer consumed this
        # journal's prefix, so deltas suffice regardless of roster changes;
        # an unresolvable trail against a gapped (pruned or
        # checkpoint-installed) journal needs a checkpoint even when the
        # peer did not ask, because a replay would silently omit retired
        # history.
        # An empty trail is position zero by definition — no store access,
        # which also keeps a not-yet-activated serving store out of the
        # decision path for first-contact pulls.
        resume_position = 0
        if resume_trail:
            resume_position = await asyncio.to_thread(
                scheduler.store.resume_ref, resume_trail
            )
        journal_gap = await asyncio.to_thread(scheduler.store.journal_gap)
        serve_checkpoint = _serve_checkpoint_decision(
            resume_position, include_checkpoint, journal_gap
        )
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
                if serve_checkpoint:
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
                        compat=peer_digest,
                        resume=resume_trail,
                    ),
                    peer_pub,
                    telemetry_channel="relay",
                    telemetry_mode="checkpoint" if serve_checkpoint else "delta",
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
                            mode="checkpoint" if serve_checkpoint else "delta",
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

    async def _handle_blob(self, token: str, message: dict):
        """Serve requested attachment objects over the relay channel."""
        from tools.network.fleet_sync.blob_transport import (
            MAX_BLOB_REQUEST_DIGESTS,
            iter_blob_frames,
        )

        scheduler = self.scheduler
        assert scheduler is not None
        if set(message) != {"v", "op", "digests", "hello"} \
                or message.get("v") != PROTOCOL_VERSION:
            raise FleetRelaySyncError("fleet blob request has unknown fields")
        digests = message.get("digests")
        if (
            not isinstance(digests, list)
            or not digests
            or len(digests) > MAX_BLOB_REQUEST_DIGESTS
            or not all(
                isinstance(item, str) and len(item) == 64
                and all(ch in "0123456789abcdef" for ch in item)
                for item in digests
            )
        ):
            raise FleetRelaySyncError("fleet blob request digests are malformed")
        hello = canonical_json(message.get("hello"))
        peer_pub, _private, server_hello, _transcript = (
            scheduler.authenticator.accept_client(hello, session=token)
        )
        current_epoch = scheduler._current_epoch()
        db_path = scheduler.config.personal_db_path

        async def stream():
            yield canonical_json({
                "v": PROTOCOL_VERSION,
                "kind": "fleet.server-hello",
                "hello": _json(server_hello, "fleet server hello"),
                "roster_epoch": current_epoch,
            })
            frames = iter_blob_frames(db_path, list(digests))
            while True:
                scheduler.authenticator.authorize(peer_pub)
                frame = await asyncio.to_thread(next, frames, None)
                if frame is None:
                    return
                yield frame

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
        resume_trail = await asyncio.to_thread(
            fleet_sync_telemetry.read_resume_breadcrumbs,
            route.origin_machine_pub,
        )
        local_digest = await asyncio.to_thread(
            SQLiteFleetSyncStore(_org_db_path("personal")).compatibility_digest
        )
        request = canonical_json({
            "v": PROTOCOL_VERSION,
            "op": PULL_OP,
            "roster_epoch": epoch,
            "checkpoint": include_checkpoint,
            "compat": local_digest,
            "resume": [
                encode_breadcrumb(breadcrumb) for breadcrumb in resume_trail
            ],
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
                    through_breadcrumb,
                ) = decode_done(raw)
                if delta_count != expected_count \
                        or delta_digest.hexdigest() != expected_digest:
                    raise FleetRelaySyncError("fleet relay delta digest mismatch")
                _ = remote_epoch
                metrics["acknowledged_transaction_ref"] = (
                    through_transaction_ref
                )
                if through_breadcrumb is not None:
                    metrics["acknowledged_breadcrumb"] = (
                        encode_breadcrumb(through_breadcrumb)
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
                # A server may initiate a checkpoint this machine did not
                # request: an unresolvable trail against a pruned journal
                # makes a checkpoint the only honest recovery, and the
                # stream below verifies it exactly like a requested one.
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
        # Best-effort: a drain failure never fails the pull that preceded
        # it, but it is logged rather than swallowed.
        try:
            await _drain_attachments_via_relay(
                route, auth, ws_base, token, envelope
            )
        except Exception:
            logger.warning(
                "fleet relay attachment drain failed", exc_info=True
            )
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)
    return metrics


async def _drain_attachments_via_relay(
    route, auth, ws_base: str, token: str, envelope: dict
) -> None:
    """Fetch quarantined attachment bytes from the serving peer and drain."""
    from tools.network.fleet_sync.blob_transport import (
        BlobReceiver,
        MAX_BLOB_REQUEST_DIGESTS,
    )

    store_api = SQLiteFleetSyncStore(_org_db_path("personal"))
    entries = await asyncio.to_thread(store_api.attachment_backlog)
    if not entries:
        return
    digests = sorted({entry.digest for entry in entries})[
        :MAX_BLOB_REQUEST_DIGESTS
    ]
    blob_store = await asyncio.to_thread(store_api.blob_store_root)
    receiver = BlobReceiver(blob_store)
    channel = await ViewerChannel.connect(
        ws_base,
        token,
        root_pub=envelope["root_pub"],
        org=envelope["org"],
    )
    try:
        private, hello = auth.build_client_hello(token)
        client_eph = _json(hello, "fleet client hello")["eph_pub"]
        request = canonical_json({
            "v": PROTOCOL_VERSION,
            "op": BLOB_OP,
            "digests": digests,
            "hello": _json(hello, "fleet client hello"),
        })
        await channel.send_message(request)
        saw_hello = False
        async for raw, _final in channel.recv_message_stream():
            if not saw_hello:
                first = _json(raw, "fleet server hello envelope")
                if first.get("kind") == "fleet.server-error":
                    raise FleetRelaySyncError(
                        f"fleet server refused: {first.get('error', 'unknown reason')}"
                    )
                if set(first) != {"v", "kind", "hello", "roster_epoch"} \
                        or first.get("kind") != "fleet.server-hello":
                    raise FleetRelaySyncError(
                        "fleet server hello envelope is malformed"
                    )
                auth.verify_server(
                    canonical_json(first["hello"]),
                    session=token,
                    client_eph=client_eph,
                    expected_machine_pub=route.origin_machine_pub,
                )
                saw_hello = True
                continue
            await asyncio.to_thread(receiver.feed, raw)
            if receiver.done:
                break
        if not receiver.done:
            raise FleetRelaySyncError(
                "fleet blob stream ended without terminal frame"
            )
        cleared = await asyncio.to_thread(
            store_api.drain_attachments, entries
        )
        logger.info(
            "fleet relay attachment drain: %d adopted, %d cleared, %d missing",
            len(receiver.adopted), cleared, len(receiver.missing),
        )
    finally:
        receiver.close()
        await channel.close()


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
        if "schema mismatch" in text:
            return "schema_mismatch"
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
                # Prefer the direct path: when a direct pull from this peer
                # succeeded within the freshness window, this relay tick is
                # redundant traffic through the public relay. Direct failure
                # simply lets the window lapse, so relay resumes within one
                # poll. The transition is logged once per flip, not per tick.
                direct_fresh = await asyncio.to_thread(
                    fleet_sync_telemetry.direct_pull_fresh,
                    route.origin_machine_pub,
                    window_s=DIRECT_FRESHNESS_WINDOW_S,
                )
                if direct_fresh != getattr(self, "_deferring_to_direct", False):
                    self._deferring_to_direct = direct_fresh
                    logger.info(
                        "fleet relay sync %s: %s",
                        route.origin_machine_pub[:12],
                        "deferring to fresh direct path"
                        if direct_fresh else "relay path active",
                    )
                if direct_fresh:
                    self.last_result = {
                        "outcome": "skipped",
                        "reason": "direct-path-fresh",
                        "at": time.time(),
                    }
                    delay = 0.5
                    await asyncio.sleep(10.0)
                    continue
                include_checkpoint = not await asyncio.to_thread(
                    _has_local_sync_state,
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


def _serve_checkpoint_decision(
    resume_position: int, requested: bool, journal_gap: bool
) -> bool:
    """A resolvable trail always means deltas; an unresolvable one means a
    checkpoint when the peer asked or when replay would omit retired
    history."""
    return resume_position == 0 and (requested or journal_gap)


def _has_local_sync_state(machine_pub: str, root_pub: str) -> bool:
    """Whether this machine holds any applied sync state at all.

    A machine with state syncs by deltas: its breadcrumb trail is
    epoch-independent continuity proof, so a roster change must never force
    a fleet-wide re-checkpoint (the old check keyed receipts on the current
    epoch and did exactly that). State is a checkpoint receipt from ANY
    epoch, or any applied/authored transaction. The peer arguments are kept
    for call-site continuity; state is a property of this machine, not of
    one peer.
    """
    del machine_pub, root_pub
    path = _org_db_path("personal")
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            receipt = conn.execute(
                "SELECT 1 FROM fleet_sync_peer_state "
                "WHERE checkpoints_received>0 LIMIT 1"
            ).fetchone()
            if receipt is not None:
                return True
            applied = conn.execute(
                "SELECT 1 FROM fleet_sync_transactions LIMIT 1"
            ).fetchone()
            return applied is not None
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
