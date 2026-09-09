"""The connector's fleet runtime: warm credential and the direct listener.

Fleet synchronization does NOT ride the relay's viewer-link path. That path
is broadcast-shaped -- a bounded per-viewer writer queue that DROPS a slow
consumer -- and routes a link to the least-loaded tunnel in the org rather
than the addressed machine. Both are correct for a viewer and wrong for bulk
replication between two named machines, and it killed a multi-GB transfer in
production before it was removed.

Peers are reached over the direct listener bound here, or over the directed
carrier (credit-based backpressure) once that lands. The invitation link is
enrollment-only: a NEW machine joining. Once a machine is enrolled and
authenticated it never relies on a public invite again.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from pathlib import Path

from tools.graph.db import _org_db_path
from tools.network import (
    fleet_roster,
    fleet_runtime,
    fleet_sync_telemetry,
)
from tools.network.fleet_sync_scheduler import (
    discover_org_sync_scopes,
    materialize_org_scopes_from_roster,
    FleetSyncRuntimeConfig,
    FleetSyncScheduler,
    SQLiteFleetSyncStore,
    roster_epoch,
)


logger = logging.getLogger(__name__)


def _materialize_then_discover_org_scopes():
    """materialize_org_scopes_from_roster() then discover_org_sync_scopes().

    The scheduler's ``sync_scopes`` callback: a fresh member first creates the
    org DB stubs its synced org roster names, so discovery returns them and the
    org databases actually synchronise (they are never created by the sync
    layer). Idempotent; on home it creates nothing."""
    try:
        newly = materialize_org_scopes_from_roster()
        if newly:
            logger.info(
                "fleet org roster: materialised %d org scope(s): %s",
                len(newly), ", ".join(newly),
            )
    except Exception:
        logger.exception("materialize_org_scopes_from_roster failed; continuing")
    return discover_org_sync_scopes()


PROTOCOL_VERSION = 1

#: Whole-pull completion deadline. Bounds connect+authenticate+transfer of one
#: pull attempt so a peer that accepts and never answers cannot hang the puller
#: forever; the run loop's backoff retries after a timeout. Mid-transfer
#: inactivity is the stream liveness policy's job — this is the outer floor.
#:
#: 30 minutes, not less: a bootstrap sweep over a tombstone-bloated catalog
#: (708k rows, ~half uncollected tombstones — semantic GC unimplemented,
#: auto-yl0r6) can run long on real hardware. Timing out mid-sweep is the worst
#: outcome: the receiver walks away, retries, and STACKS another sweep on the
#: server. The deadline must comfortably exceed
#: one honest build+transfer; shrinking the catalog (GC) is the real cure.
PULL_DEADLINE_S = 1800.0
PULL_OP = "fleet.sync.pull"
#: A direct-path success younger than this makes a relay pull redundant.
DIRECT_FRESHNESS_WINDOW_S = 30.0
BLOB_OP = "fleet.sync.blob"
CONTROL_OP = "fleet-runtime"
_REQUEST_FIELDS = {
    "v", "op", "roster_epoch", "bootstrap", "compat", "resume", "hello",
}


class FleetRelaySyncError(RuntimeError):
    """The route, Fleet proof, or sync stream was invalid."""


def _json(raw: object, what: str) -> dict:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise FleetRelaySyncError(f"{what} is not JSON") from exc
    if not isinstance(value, dict):
        raise FleetRelaySyncError(f"{what} must be an object")
    return value


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
        #: Only the personal connector binds the machine-wide inbound
        #: listener; org connectors get identity and caps but never the port.
        self._owns_inbound_listener: bool = False
        #: How many sync pulls this process has turned away because it holds no
        #: credential (scheduler is None), and when the first one arrived. This
        #: is the "764 requests refused since 8pm" the profile sync flag reports:
        #: an unarmed serving process looks identical to a healthy one on every
        #: other check, and the count is the difference between "nothing is
        #: happening" and "something is trying and failing". Reset by configure()
        #: — a fresh arm starts a new "since".
        self.locked_refusals: int = 0
        self.first_locked_refusal_at: float | None = None
        #: One serve per scope at a time. A client that retries while its
        #: previous serve is still running must queue behind it, not stack
        #: another full-database sweep beside it — N stacked sweeps
        #: GIL-starve each other so NONE finishes inside the client
        #: deadline, and the retry cadence turns that into a permanent
        #: 100%-CPU wedge (observed live 2026-09-06).
        #: Live pull/blob streams right now. Reported in connector-status so
        #: the supervisor can DRAIN a stale-code incumbent (wait for zero, or
        #: a deadline) instead of severing mid-transfer on every merge.
        self.active_streams: int = 0
        #: monotonic() at the last frame any stream yielded. The supervisor
        #: honors a lame duck only when this is RECENT: active_streams alone
        #: proved unreliable (a generator abandoned mid-yield held the count
        #: at 1 for 5+ minutes with no stream, live 2026-09-06), and a stuck
        #: counter must not keep a stale-code connector alive.
        self.last_stream_activity: float | None = None
        #: Listeners of replaced schedulers, stopped on the next ensure pass.
        self._retired_listeners: list = []
        #: (host, port) the direct listener is bound to right now, or None.
        self.direct_listener: tuple[str, int] | None = None

    def _touch_stream_activity(self) -> None:
        self.last_stream_activity = time.monotonic()

    def stream_activity_age_s(self) -> float | None:
        if self.last_stream_activity is None:
            return None
        return max(0.0, time.monotonic() - self.last_stream_activity)

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
        # The direct listener lives HERE by default (fleet-direct row
        # serve_in=connector): this process already streams relay serves
        # and carries no operator UI. The
        # listener itself is bound by ensure_direct_listener() on the
        # connector's loop, never in this synchronous configure().
        from tools.network import fleet_direct_config

        direct = fleet_direct_config.load()
        listen_host, listen_port = fleet_direct_config.listener_bind(
            direct, "connector"
        )
        # ONE INBOUND LISTENER PER MACHINE, and only the personal connector
        # owns it.
        #
        # Outbound tunnels are per-organization by design: each is
        # authenticated AS that org by its own tunnel:serve certificate, and
        # one socket cannot present four org identities. The INBOUND direct
        # listener is the opposite — it is machine-wide and multiplexes every
        # org by channel (FleetSyncScheduler / org_channel_for), so exactly one
        # process may bind it.
        #
        # Every connector computed the same bind from the same machine-scoped
        # row, so four org connectors raced for port 9410. Whichever started
        # first won and the rest died with EADDRINUSE — measured 2026-09-09,
        # anchore holding the port while the PERSONAL connector, the one that
        # actually owns fleet sync, could not bind at all.
        if not self._owns_inbound_listener:
            listen_host, listen_port = fleet_direct_config.DEFAULT_LISTEN_HOST, 0
        config = FleetSyncRuntimeConfig(
            machine_key=credential.process_key,
            roster_machine_pub=credential.machine_pub,
            delegation_cert=credential.delegation_cert,
            require_delegation=True,
            personal_root_pub=root_pub,
            roster_entries=lambda: fleet_roster.load_entries(org=None),
            peer_addresses=lambda: {},
            personal_db_path=_org_db_path("personal"),
            listen_host=listen_host,
            listen_port=listen_port,
            telemetry_recorder=fleet_sync_telemetry.record_iteration,
            # Materialise org DB stubs from the synced org roster before
            # discovery, so the direct/tunnel scheduler (like the relay pull)
            # bootstraps a fresh member's org scopes rather than only seeing
            # whatever files already exist.
            sync_scopes=_materialize_then_discover_org_scopes,
        )
        scheduler = FleetSyncScheduler(config)
        scheduler._roster_snapshot = entries
        scheduler.authenticator.authorize(credential.machine_pub)
        # Direct serves count as this connector's live streams, so the
        # supervisor drains (lame duck) instead of recycling mid-transfer.
        scheduler.stream_observer = _DirectStreamObserver(self)
        previous = self.scheduler
        self.scheduler = scheduler
        if previous is not None and previous.server.running:
            # A re-arm replaces the scheduler; the old listener must go so
            # the new one can take the port on the next ensure pass.
            self._retired_listeners.append(previous.server)
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

    async def ensure_direct_listener(self) -> tuple[str, int] | None:
        """Bind, rebind, or stop this process's direct listener to match the
        armed scheduler's configured bind. Idempotent; called from the
        connector's loop at startup and on a slow cadence, so a row written
        after arming or a re-arm takes effect without a restart. Returns the
        live (host, port) or None."""
        for server in list(self._retired_listeners):
            with contextlib.suppress(Exception):
                await server.stop()
            self._retired_listeners.remove(server)
        scheduler = self.scheduler
        if scheduler is None:
            self.direct_listener = None
            return None
        server = scheduler.server
        wanted_port = scheduler.config.listen_port
        if wanted_port <= 0:
            if server.running:
                await server.stop()
                logger.info("fleet direct listener stopped (bind disabled)")
            self.direct_listener = None
            return None
        if server.running:
            self.direct_listener = (server.host, server.port)
            return self.direct_listener
        try:
            port = await server.start()
        except OSError as exc:
            logger.warning(
                "fleet direct listener could not bind %s:%d: %s",
                scheduler.config.listen_host, wanted_port, exc,
            )
            self.direct_listener = None
            return None
        self.direct_listener = (scheduler.config.listen_host, port)
        logger.info(
            "fleet direct listener bound %s:%d in the connector "
            "(roster-authenticated; serves sweeps and deltas here, "
            "never on the dashboard loop)",
            scheduler.config.listen_host, port,
        )
        return self.direct_listener

    async def direct_listener_loop(self, interval_s: float = 15.0) -> None:
        """Keep the direct listener matched to config for the process life."""
        while True:
            try:
                await self.ensure_direct_listener()
            except Exception:
                logger.warning("fleet direct listener maintenance failed",
                               exc_info=True)
            await asyncio.sleep(interval_s)

    def set_owns_inbound_listener(self, owns: bool) -> None:
        """Declare whether THIS connector process owns the machine-wide
        inbound direct listener. Set once at startup from the connector's
        scope; the personal connector owns it, org connectors do not."""
        self._owns_inbound_listener = bool(owns)

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
            # LOUD, because this is the path that actually happened and it was
            # the only silent one. Home stopped re-arming at 2026-09-09
            # 07:19:33Z and every connector restart after that came up with no
            # machine key -- which downgrades its hello to v1 and puts it on
            # the shared empty-machine relay slot. Nothing said so; the last
            # success was visible in the log and the failures were not, which
            # is the worst possible arrangement for diagnosis.
            logger.warning(
                "fleet runtime warm cache is EMPTY — this connector starts "
                "UNARMED (no machine key), so its hello degrades to v1 and it "
                "shares the legacy empty-machine relay slot. A fresh operator "
                "unlock re-mints it")
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

#: (Removed 2026-09-06.) A periodic faulthandler.dump_traceback_later here
#: correlated with two connector deaths mid-dump: that watchdog dumps from a C
#: thread WITHOUT the GIL while asyncio.to_thread workers are created and torn
#: down under heavy transfer — the case CPython documents as unsafe. The
#: CPU-gated sampler in link_serving (synchronous dump, GIL held) and the
#: SIGUSR1 on-demand dump replace it.
def _arm_stall_dump() -> None:
    return None


def _disarm_stall_dump() -> None:
    return None


#: Websocket pong deadline for the PULLER's relay connection. Under a bulk
#: receive the relay's pong queues behind data frames, so the
#: library default (20s) closed a 2.27GB transfer at minute 3.5 with 1011
#: 'keepalive ping timeout' while frames were still arriving (SJC log,
#: 2026-09-06). The 60s frame-silence rule remains the liveness check for a
#: receiving stream; this only stops pong latency from killing it.
PULL_PING_TIMEOUT_S = 90.0

class _DirectStreamObserver:
    """Bridges direct-path serves into the connector's stream accounting."""

    def __init__(self, runtime: "ConnectorFleetRuntime"):
        self._runtime = runtime

    def begin(self) -> None:
        self._runtime.active_streams += 1
        self._runtime._touch_stream_activity()

    def touch(self) -> None:
        self._runtime._touch_stream_activity()

    def end(self) -> None:
        self._runtime.active_streams = max(0, self._runtime.active_streams - 1)
        self._runtime._touch_stream_activity()


connector_runtime = ConnectorFleetRuntime()




def _scope_db_path(scope: str) -> Path:
    """The local database path one sync scope replicates into."""
    if scope == "personal":
        return _org_db_path("personal")
    scopes = discover_org_sync_scopes()
    if scope not in scopes:
        raise FleetRelaySyncError(f"unknown fleet sync scope: {scope!r}")
    return scopes[scope]


_activated_scope_paths: set[Path] = set()

#: Per-peer declared sync protocol version for relay pulls. A server that
#: refused a v4 declaration before its hello is retried at v3 for the rest
#: of this process; a restart re-probes v4. Only wire efficiency rides on
#: this, never correctness.
_relay_sync_versions: dict[str, int] = {}


def _scoped_store(scope: str, machine_pub: str) -> SQLiteFleetSyncStore:
    """The scope's client store, with fleet writers activated once per path.

    Mirrors the direct scheduler's ``_store_for``: organization databases
    share the graph schema, so the same policy audit applies and activation
    fails closed on any unpoliced table. The personal database is prepared
    by the production migration and is never activated here.
    """
    path = _scope_db_path(scope)
    if scope != "personal" and path not in _activated_scope_paths:
        from tools.graph.db import GraphDB

        graph = GraphDB(path)
        try:
            graph.activate_fleet_sync_writers(machine_pub)
        finally:
            graph.close()
        _activated_scope_paths.add(path)
    return SQLiteFleetSyncStore(path)




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
