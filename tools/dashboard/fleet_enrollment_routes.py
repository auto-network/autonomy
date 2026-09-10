"""Operator Dashboard API for machine-neutral fleet enrollment.

The browser owns every personal-root signature.  These routes register its
signed invitation, display pending public requests, and verify the two signed
approval records.  They never accept a root seed or signing key.  Approval and
decline require the human Dashboard session cookie; a session bearer can
authenticate an API caller but cannot exercise personal-root authority.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import time

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from tools.dashboard import (
    fleet_enrollment_service,
    identity_routes,
    link_serving,
    link_serving_supervisor,
    unlock_routes,
)
from tools.graph.db import _org_db_path
from tools.network import (
    fleet_invite,
    fleet_relay_sync,
    fleet_runtime,
    fleet_roster,
    fleet_sync_scheduler,
    fleet_tunnel_server,
    machine_boot,
)
from tools.network.fleet_enrollment_client import FleetJoinStateStore


def _operator_required(request: Request) -> JSONResponse | None:
    if unlock_routes.session_from_request(request) is None:
        return JSONResponse(
            {
                "ok": False,
                "error": (
                    "fleet enrollment approval requires an unlocked human "
                    "Dashboard session"
                ),
            },
            status_code=401,
        )
    return None


async def register_invite(request: Request) -> JSONResponse:
    denied = _operator_required(request)
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "body must be JSON"}, status_code=400)
    expected = {"org", "target_uuid", "grant_token", "invite"}
    if not isinstance(body, dict) or set(body) != expected:
        return JSONResponse(
            {"ok": False, "error": f"body must carry exactly {sorted(expected)}"},
            status_code=400,
        )
    org = body["org"]
    token = body["grant_token"]
    if not isinstance(org, str) or not org:
        return JSONResponse({"ok": False, "error": "org must be a non-empty slug"}, status_code=400)
    try:
        invite = fleet_invite.FleetInvite.from_dict(body["invite"])
        fleet_invite.verify(invite)
        personal = identity_routes._personal_member()
        anchor = (personal.payload if personal is not None else {}).get("root_pub")
        if not isinstance(anchor, str) or invite.personal_root_pub != anchor:
            raise ValueError("invite is not anchored to the stored personal identity")
        grant = link_serving.check_grant(token, org=org, now=time.time())
        if (
            grant is None
            or grant.get("target_type") != "fleet:join"
            or grant.get("target_uuid") != body["target_uuid"]
        ):
            raise ValueError("grant is unavailable or is not this fleet invitation")
        fleet_enrollment_service.FleetEnrollmentStore().register_invite(
            target_uuid=body["target_uuid"],
            grant_token=token,
            invite=invite,
        )
    except (ValueError, TypeError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({
        "ok": True,
        "target_uuid": body["target_uuid"],
        "invite_id": invite.invite_id,
        "rendezvous": invite.rendezvous,
    })


async def pending_requests(request: Request) -> JSONResponse:
    denied = _operator_required(request)
    if denied is not None:
        return denied
    target_uuid = request.query_params.get("target_uuid")
    try:
        rows = fleet_enrollment_service.FleetEnrollmentStore().list_pending(
            target_uuid
        )
    except (ValueError, TypeError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({
        "ok": True,
        "requests": [
            {
                "request_id": row.request_id,
                "target_uuid": row.target_uuid,
                "request": row.request.to_dict(),
                "channel_binding": row.channel_binding,
                "verification_code": row.verification_code,
                "status": row.status,
                "source_approval_id": row.source_approval_id,
                "last_error_code": row.last_error_code,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
            }
            for row in rows
        ],
    })


def _local_completion_state():
    state = FleetJoinStateStore()
    recovery = state.latest_any()
    if recovery is None:
        return state, None, None
    return state, recovery, state.load_delivery(recovery.request_id)


def _runtime_context() -> tuple[str, fleet_roster.RosterEntry] | None:
    machine_id_value = machine_boot.machine_id(org="machine")
    root_pub = fleet_tunnel_server._personal_root_pub()
    if machine_id_value is None or root_pub is None:
        return None
    entry = fleet_roster.own_entry(
        machine_id_value, anchor_root_pub=root_pub, org=None
    )
    if entry is None:
        return None
    return root_pub, entry


def _ensure_fleet_catalog(machine_pub: str) -> None:
    """Activate authored personal-DB capture under the durable machine id."""
    from tools.graph.db import GraphDB
    from tools.network.fleet_sync.compaction import WatermarkError

    path = _org_db_path("personal")
    GraphDB.close_pooled_path(path)
    db = GraphDB(path, attach_fleet_sync=False)
    try:
        already_active = db.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE 'fleet_sync_%' LIMIT 1"
        ).fetchone() is not None
        if not already_active:
            try:
                db.migrate_fleet_sync_catalog(machine_pub)
            except WatermarkError as exc:
                if "capture triggers present" not in str(exc):
                    raise
        db.activate_fleet_sync_writers(machine_pub)
    finally:
        db.close()


def _reachability_binding():
    """The personal org's registry binding (registry_url + org_uuid), or None.

    None until the personal org has published/registered on auto.network; then
    reachability discovery is available and the browser mints a reachability cert.
    """
    try:
        from tools.dashboard.link_approvals import _load_binding

        binding, _err = _load_binding(None)
        return binding
    except Exception:
        return None


def _fleet_advertise_addrs():
    """This machine's externally-reachable sync-listener URLs.

    The ``autonomy.machine.fleet-direct`` row is the durable source; the
    ``AUTONOMY_FLEET_ADVERTISE_ADDRS`` environment variable (comma-separated
    ws/wss URLs) is still unioned in. Read fresh on every reachability
    refresh, so a row written after unlock is announced within one interval.
    """
    from tools.network import fleet_direct_config

    return fleet_direct_config.advertise_addrs()


#: The live ReachabilityCache of the most recent runtime activation, kept so
#: the fleet verdict can report announce/discovery state without forcing a
#: registry round trip.
_reachability_cache = None


def _fleet_env_peers():
    """Manual peer map from ``AUTONOMY_FLEET_PEERS`` (machine_pub -> ws urls).

    Supplements/overrides registry discovery, so peers can be supplied before the
    personal org registers and stay operator-overridable.
    """
    raw = os.environ.get("AUTONOMY_FLEET_PEERS")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for pub, urls in data.items():
        if isinstance(pub, str) and isinstance(urls, (list, tuple)):
            cand = [u for u in urls if isinstance(u, str) and u]
            if cand:
                out[pub] = cand
    return out


def _reachability_peer_addresses(credential, root_pub, *, pull_direct=True):
    """Build the scheduler's peer_addresses: registry discovery + env override.

    ``pull_direct=False`` keeps the cache refreshing (this machine still
    announces itself and still discovers peers for the verdict) but hands
    the scheduler no addresses, so this dashboard never pulls over direct.

    A throttled ReachabilityCache announces this machine and resolves roster
    peers via node:announce/node:lookup (using the credential's machine key +
    reachability cert). When those are absent (org unregistered) the cache yields
    {}; AUTONOMY_FLEET_PEERS is unioned on top either way.
    """
    global _reachability_cache
    from tools.network import fleet_direct_config, fleet_reachability

    cache = fleet_reachability.ReachabilityCache(
        binding_getter=_reachability_binding,
        machine_key_getter=lambda: credential.machine_key,
        cert_getter=lambda: credential.reachability_cert,
        roster_getter=lambda: list(
            fleet_roster.resolve(
                fleet_roster.load_entries(org=None), anchor_root_pub=root_pub
            ).keys()
        ),
        advertise_addrs=_fleet_advertise_addrs,
        # Off the event loop: a due refresh runs on its own thread and the
        # scheduler gets the last map at once (auto-8dw0w).
        background=True,
    )
    _reachability_cache = cache

    def peers():
        merged = dict(cache.peers())
        merged.update(_fleet_env_peers())
        # RE-READ the row, do not close over the value. The scheduler calls
        # this every iteration, so the only reason a pull_direct change needed
        # a re-arm was that the answer was frozen at arm time.
        #
        # Live on home 2026-09-10: the operator authorized the flip to True,
        # host-0906-222509 wrote the row at 02:00:25Z, and nothing dialled —
        # every armed process had captured False seconds to minutes earlier
        # (connectors 02:00:03, the dashboard puller 01:39:25). No failed dial,
        # no candidate error: the scheduler simply held an empty address list
        # and no amount of polling could change it. A durable Setting the
        # operator changes and that silently does not apply is the same defect
        # class as the rest of tonight, and it is worse here because the
        # symptom is silence.
        #
        # The read is a local Settings lookup on a ~13s cycle. A failure to
        # read must not strand the tier either way, so it falls back to the
        # value captured at arm time.
        try:
            allow_direct = fleet_direct_config.load().pull_direct
        except Exception:
            allow_direct = pull_direct
        if not allow_direct:
            return {}
        return merged

    return peers


def _dashboard_runtime_cache() -> fleet_relay_sync.FleetRuntimeWarmCache:
    """The Dashboard's own copy of the Fleet runtime credential (auto-5er0n).

    Stored in the same ramfs warm cache the serving connector uses (auto-ixwr3)
    but under a DISTINCT file name, keyed by the same registry ``org_uuid`` the
    connector serves under. The connector's own entry is written ONLY on the
    machine selected to serve the tunnel — on any other machine no connector
    process runs, ``link_serving.py`` never executes, and that file never
    exists — so the Dashboard must read its own entry, never the connector's.
    """
    binding = _reachability_binding()
    org_uuid = binding.get("org_uuid") if binding else None
    return fleet_relay_sync.FleetRuntimeWarmCache(
        org_uuid, name_prefix="fleet-dashboard-runtime"
    )


def serving_org_targets() -> list:
    """Every ORG scope this machine is provisioned to serve, with the two ids
    the browser needs to derive that org's serving machine key (auto-e2ufw):
    the registry ``org_uuid`` and the org's ledger ``genesis_id``.

    Only orgs. The personal scope keeps the fleet machine key as its hello
    identity: its org is the operator's own, so binding it to the operator's
    own fleet key discloses nothing that the roster does not already say, and
    re-keying the one connector that currently carries Fleet sync buys no
    unlinkability for real risk. Collaborative orgs are the whole point —
    without this, the same physical machine shows one correlatable key to
    every org's relay.

    Every failure mode is a skip, not a raise: this list decides what the
    browser is ASKED to derive, and an org that cannot answer here simply
    keeps the behaviour it has today.
    """
    from tools.dashboard import link_serving_supervisor as _sup
    from tools.dashboard.link_approvals import _load_binding
    from tools.network.ledger import LedgerStore, org_ledger_db_path

    targets = []
    try:
        scopes = _sup._discover_startup_orgs()
    except Exception:
        return targets
    for scope in scopes:
        if scope is None or scope == "personal":
            continue
        try:
            state = _sup.serve_cert_state(scope)
            if state.get("status", "missing") == "missing":
                continue          # never provisioned to serve — not a fault
            binding, _err = _load_binding(scope)
            org_uuid = binding.get("org_uuid") if binding else None
            if not org_uuid:
                continue
            path = org_ledger_db_path(scope)
            if not path.exists():
                continue
            with LedgerStore(path) as store:
                genesis_id = store.ledger.genesis_id
            if not genesis_id:
                continue
        except Exception:
            continue
        targets.append({
            "scope": scope,
            "org_uuid": org_uuid,
            "genesis_id": genesis_id,
        })
    return targets


def _arm_serving_orgs(base_payload: dict, seeds: object) -> None:
    """Give every serving org connector its own runtime credential.

    THE POINT OF THIS FUNCTION: an org connector cannot start without a
    machine key, and the only way it ever received one was the control socket
    it serves itself — which it cannot serve until it starts. Nothing broke
    that circle, so activate_local_runtime published for org=None alone and
    every org connector on sjc-2 died at launch for an hour, once ramfs
    cleared, with "no runtime machine key is available for this scope".

    Writing the connector's warm cache from HERE breaks it: the cache is the
    file the connector already reads at launch (link_serving.py, attach_warm_
    cache + rearm_from_cache), so the next watchdog respawn finds a key
    without anyone having to be present. The control-socket publish stays as
    a best-effort live rotation for a connector that is already up.

    ramfs, 0600, and cleared by a reboot — so a reboot still fails closed to a
    human unlock, which is the property the cache was built to keep.
    """
    if not isinstance(seeds, dict) or not seeds:
        return
    logger = logging.getLogger(__name__)
    for target in serving_org_targets():
        seed_hex = seeds.get(target["org_uuid"])
        if not isinstance(seed_hex, str) or not seed_hex:
            continue
        org_payload = dict(base_payload)
        org_payload["serving_machine_private_seed"] = seed_hex
        try:
            fleet_relay_sync.FleetRuntimeWarmCache(target["org_uuid"]).store(
                org_payload
            )
        except Exception:
            # No ramfs, or a cache this process cannot write: the org keeps the
            # behaviour it has today. Never fails an activation that otherwise
            # succeeded.
            logger.warning(
                "could not arm the warm cache for serving org %s",
                target["scope"], exc_info=True,
            )
            continue
        try:
            fleet_relay_sync.publish_connector_runtime(
                org_payload, org=target["scope"]
            )
        except Exception as exc:
            # Expected whenever the connector is not up yet — which is the
            # case this whole function exists to fix. The cache above is what
            # arms it; this was only the fast path.
            logger.info(
                "serving org %s is not running yet; it will arm from the warm "
                "cache on its next launch (%s)", target["scope"], exc,
            )


def rearm_local_runtime_from_cache() -> bool:
    """Replay the Dashboard's cached Fleet runtime credential at startup.

    Returns ``False`` when no payload is cached (a machine that was never
    unlocked, or whose ramfs was cleared by a reboot — both correctly stay
    locked until a human unlocks). Otherwise replays the RAW browser payload
    through :func:`_activate_runtime` and returns ``True``.

    Replaying the raw payload rather than a reconstructed credential is
    deliberate: ``_activate_runtime`` re-runs ``from_browser_payload``, which
    verifies the payload against the current ``personal_root_pub`` and the live
    roster entries, so a stale or de-rostered payload fails to activate instead
    of arming the machine with a credential the fleet no longer accepts. Going
    through ``_activate_runtime`` also re-arms all three runtime consumers, so
    no consumer needs re-arming code of its own.
    """
    log = logging.getLogger(__name__)
    try:
        payload = _dashboard_runtime_cache().load()
    except Exception:
        log.warning(
            "fleet runtime replay: the dashboard runtime cache could not be "
            "READ; this machine stays locked until a human unlock",
            exc_info=True)
        return False
    if payload is None:
        # Distinct from a failed replay and from a successful one, and until
        # now indistinguishable from both: all three returned quietly.
        log.warning(
            "fleet runtime replay: NO cached payload — this machine was never "
            "unlocked, or ramfs was cleared. Connectors will start UNARMED "
            "(hello v1, shared empty-machine relay slot) until a human unlock")
        return False
    try:
        _activate_runtime(payload)
    except Exception:
        # THE PATH THAT ACTUALLY HAPPENED (2026-09-09). Home activated at
        # 13:54:07Z and every connector generation after it started cold. The
        # caller wraps this whole function in contextlib.suppress(Exception),
        # so a replay that threw — a delegation cert that has since expired,
        # a de-rostered machine — was indistinguishable from one that was
        # never attempted. Four connector restarts, no explanation anywhere.
        log.warning(
            "fleet runtime replay FAILED from a cached payload; the credential "
            "is stale (expired cert or de-rostered machine) and a fresh "
            "operator unlock is required. Connectors start UNARMED until then",
            exc_info=True)
        return False
    log.warning(
        "fleet runtime replayed from the dashboard cache — Fleet sync re-armed "
        "with nobody present")
    return True


def _activate_runtime(
    payload: object,
    *,
    root_pub: str | None = None,
    expected_entry: fleet_roster.RosterEntry | None = None,
    publish_connector: bool | None = None,
) -> fleet_runtime.FleetRuntimeCredential:
    from tools.network import fleet_sync_telemetry

    # The per-org serving seeds ride ALONGSIDE the credential, not inside it:
    # FleetRuntimeCredential.from_browser_payload takes exactly one scope's
    # material (it accepts the singular serving_machine_private_seed) and
    # rejects unknown keys, so the map is peeled here and each org gets its
    # own single-seed payload built from the same base.
    serving_seeds = {}
    if isinstance(payload, dict) and "serving_machine_private_seeds" in payload:
        payload = dict(payload)
        serving_seeds = payload.pop("serving_machine_private_seeds") or {}

    context = _runtime_context() if root_pub is None or expected_entry is None else None
    if context is None and (root_pub is None or expected_entry is None):
        raise fleet_runtime.FleetRuntimeError(
            "this Dashboard has no active Fleet roster identity"
        )
    if context is not None:
        root_pub, expected_entry = context
    assert root_pub is not None and expected_entry is not None
    entries = tuple(fleet_roster.load_entries(org=None))
    binding = _reachability_binding()
    org_uuid = binding.get("org_uuid") if binding else None
    credential = fleet_runtime.FleetRuntimeCredential.from_browser_payload(
        payload,
        personal_root_pub=root_pub,
        roster_entries=entries,
        org_uuid=org_uuid,
    )
    if credential.machine_id != expected_entry.machine_id:
        raise fleet_runtime.FleetRuntimeError(
            "fleet runtime credential names a different local machine"
        )
    # Carry the Dashboard's OWN copy of this credential across a restart
    # (auto-5er0n). It is held only in process memory otherwise, so every
    # Dashboard restart left the machine unable to pull Fleet sync until a human
    # unlocked again. The raw browser payload is stored in the same ramfs warm
    # cache the serving connector uses (auto-ixwr3) but under a DISTINCT file
    # name, and replayed at startup by rearm_local_runtime_from_cache(). A cache
    # write failure must never fail an activation that otherwise succeeded.
    with contextlib.suppress(Exception):
        _dashboard_runtime_cache().store(payload)
    _ensure_fleet_catalog(credential.machine_pub)
    from tools.network import fleet_direct_config

    # The direct tier binds where the machine-local fleet-direct row says.
    # Default (no row) is the historical loopback ephemeral listener, which
    # no peer can dial; a fixed port on a reachable interface plus advertised
    # URLs is what makes direct pulls happen instead of relay pulls.
    direct = fleet_direct_config.load()
    listen_host, listen_port = fleet_direct_config.listener_bind(direct, "dashboard")
    fleet_sync_scheduler.configure_dashboard_fleet_sync(
        fleet_sync_scheduler.FleetSyncRuntimeConfig(
            machine_key=credential.process_key,
            roster_machine_pub=credential.machine_pub,
            delegation_cert=credential.delegation_cert,
            require_delegation=True,
            personal_root_pub=root_pub,
            roster_entries=lambda: fleet_roster.load_entries(org=None),
            listen_host=listen_host,
            listen_port=listen_port,
            # Relay-discovered peer channels: a throttled ReachabilityCache
            # announces this machine (its reachability cert + machine key) and
            # resolves roster peers via node:announce/node:lookup. When the
            # personal org is not registered the credential carries no
            # reachability material, the cache yields {}, and this is byte-
            # identical to the sync-only path.
            peer_addresses=_reachability_peer_addresses(
                credential, root_pub, pull_direct=direct.pull_direct,
            ),
            # A failed pull re-looks that peer up before the next interval.
            on_peer_failure=lambda pub: (
                _reachability_cache.note_failed(pub)
                if _reachability_cache is not None else None
            ),
            personal_db_path=_org_db_path("personal"),
            telemetry_recorder=fleet_sync_telemetry.record_iteration,
            resume_cursor=(
                lambda peer, scope="personal":
                    fleet_sync_telemetry.read_resume_breadcrumbs(
                        peer, scope=scope
                    )
            ),
            # Every organization database on this machine synchronizes
            # across the personal fleet beside the personal one.
            sync_scopes=fleet_sync_scheduler.discover_org_sync_scopes,
        )
    )
    if publish_connector is None:
        # THIRD designation gate. This one is machine-local — it only decides
        # whether the LOCAL serving subprocess is handed its process credential
        # over the control socket (no fleet-wide row is written) — so a
        # non-designated machine could launch a connector and then never feed
        # it. Same predicate as the other two so the three cannot disagree.
        publish_connector = fleet_tunnel_server.tunnel_serving_permitted()[0]
    if publish_connector:
        # Serve the fleet connector on the PERSONAL tunnel (org=None →
        # personal.db), never a shared org's: the fleet is anchored on the
        # personal root — its roster, delegation cert, and this binding (loaded
        # above via _reachability_binding → _load_binding(None)) all live in the
        # personal store — so a virgin system with zero collaborative orgs still
        # has exactly one tunnel to serve on, its own. Left implicit,
        # publish_connector_runtime would fall back to shell_default_org() (a
        # UI/attribution default) and notify whatever org is cosmetically first.
        try:
            fleet_relay_sync.publish_connector_runtime(payload, org=None)
        except (
            link_serving_supervisor.TunnelUnavailable,
            fleet_relay_sync.FleetRelaySyncError,
        ) as exc:
            # The credential above is already configured and valid; only
            # the "tell the already-running connector" step failed, most
            # commonly because no connector has been provisioned for this
            # scope yet. Log it rather than raising -- failing this request
            # would also discard the credential setup that already
            # succeeded, for a step that's a notification, not a
            # precondition.
            logging.getLogger(__name__).warning(
                "activate_local_runtime: could not notify the personal "
                "serving connector (%s) -- credential is still configured",
                exc,
            )
    # Each serving ORG gets the same base credential plus ITS OWN machine key
    # (auto-e2ufw). Runs regardless of publish_connector: an org connector's
    # cache must be armed even on a machine whose personal connector is not
    # the designated fleet tunnel server, because the org tunnels are a
    # different question from who carries Fleet sync.
    _arm_serving_orgs(payload, serving_seeds)
    return credential


def _local_sync_phase() -> str:
    """Return the compact first-sync phase from durable local evidence.

    A successful delta pull is not enough for a newly enrolled Dashboard: it
    may contain only writes made after catalog activation.  The onboarding
    completion boundary is therefore a COMPLETE bootstrap sweep -- both the
    ``<= F`` and ``> F`` halves durably applied -- together with a successful
    pull from one active remote roster member in the current roster epoch.
    Until both hold the truthful state remains ``synchronizing``.
    """
    local_id = machine_boot.machine_id(org="machine")
    root_pub = fleet_tunnel_server._personal_root_pub()
    if local_id is None or root_pub is None:
        return "synchronizing"
    try:
        entries = tuple(fleet_roster.load_entries(org=None))
        active = fleet_roster.resolve(entries, anchor_root_pub=root_pub)
        local_entry = next(
            (entry for entry in active.values() if entry.machine_id == local_id),
            None,
        )
        if local_entry is None or len(active) < 2:
            return "synchronizing"
        epoch = fleet_sync_scheduler.roster_epoch(entries, root_pub)
    except Exception:
        return "synchronizing"

    path = _org_db_path("personal")
    if not path.exists():
        return "synchronizing"
    remote_keys = tuple(sorted(set(active) - {local_entry.machine_pub}))
    if not remote_keys:
        return "synchronizing"
    placeholders = ",".join("?" for _ in remote_keys)
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='fleet_sync_peer_state'"
        ).fetchone()
        if exists is None:
            return "synchronizing"
        row = conn.execute(
            "SELECT 1 FROM fleet_sync_peer_state "
            f"WHERE roster_epoch=? AND machine_public_key IN ({placeholders}) "
            "AND last_success_ns IS NOT NULL LIMIT 1",
            (epoch, *remote_keys),
        ).fetchone()
        if row is None:
            return "synchronizing"
        # A pull succeeded; the bootstrap it carried must also have finished.
        # An absent row means this store never bootstrapped by sweep, which
        # for a freshly enrolled dashboard is not yet complete.
        from tools.network.fleet_sync.sweep_receive import Phase, read_bootstrap

        state = read_bootstrap(conn)
        if state is None or state.phase is not Phase.COMPLETE:
            return "synchronizing"
        return "complete"
    except sqlite3.Error:
        return "synchronizing"
    finally:
        if "conn" in locals():
            conn.close()


async def local_sync_status(request: Request) -> JSONResponse:
    """Binary first-sync status for the post-enrollment welcome screen."""
    denied = _operator_required(request)
    if denied is not None:
        return denied
    return JSONResponse({"ok": True, "status": _local_sync_phase()})


async def resume_local_enrollment(request: Request) -> JSONResponse:
    """Advance a fresh install's existing request without a node restart.

    This route creates no request and accepts no caller-supplied identity.  It
    can only exercise the resume credential already stored in machine.db, then
    persist the parent-delivered encrypted armor and public evidence.  The
    browser still performs the password/root ceremony and machine-key proof.
    """
    from tools.init.first_run import _store_fleet_personal_armor
    from tools.network.fleet_enrollment_client import (
        FleetEnrollmentClient,
        FleetEnrollmentClientError,
    )

    state = FleetJoinStateStore()
    try:
        recovery = state.latest_any()
        if recovery is None:
            return JSONResponse({"ok": True, "status": "none"})
        if state.load_delivery(recovery.request_id) is not None:
            return JSONResponse({"ok": True, "status": "approved"})
        result = await FleetEnrollmentClient(state_store=state).resume(recovery)
        if result.status == "declined":
            state.delete(recovery.request_id)
            return JSONResponse({"ok": True, "status": "declined"})
        if result.status == "expired":
            return JSONResponse({"ok": True, "status": "expired"})
        if result.status == "unavailable":
            # The home dashboard reached us and named a specific invitation
            # fault. Give the joiner the exact cause + fix, not a generic 502.
            messages = {
                "invite_inactive": "Your invitation link was deactivated on "
                    "the home dashboard. Reactivate it there to finish setup.",
                "invite_expired": "Your invitation link has expired. Create a "
                    "new invite on the home dashboard and install again.",
                "invite_unknown": "The home dashboard no longer recognizes "
                    "this invitation link. Create a new invite there.",
            }
            reason = result.reason or "invite_inactive"
            return JSONResponse({
                "ok": False, "status": "unavailable", "reason": reason,
                "message": messages.get(
                    reason,
                    "This invitation link is not currently active on the home "
                    "dashboard. Reactivate it there to finish setup.",
                ),
            })
        if result.status == "approved":
            if (
                result.delivery is None
                or result.personal_root_armor is None
                or result.personal_root_created_at is None
                or result.personal_root_updated_at is None
            ):
                raise FleetEnrollmentClientError(
                    "approved fleet enrollment returned incomplete delivery"
                )
            _store_fleet_personal_armor(
                result.personal_root_armor,
                expected_root_pub=recovery.invite.personal_root_pub,
                source_created_at=result.personal_root_created_at,
                source_updated_at=result.personal_root_updated_at,
            )
            state.save_delivery(recovery.request_id, result.delivery)
            unlock_routes.bust_enforce_cache()
            return JSONResponse({"ok": True, "status": "approved"})
        return JSONResponse({"ok": True, "status": "pending"})
    except (FleetEnrollmentClientError, ValueError, TypeError) as exc:
        return JSONResponse(
            {"ok": False, "status": "error", "error": str(exc)},
            status_code=502,
        )


async def local_completion_context(request: Request) -> JSONResponse:
    denied = _operator_required(request)
    if denied is not None:
        return denied
    try:
        _state, recovery, delivery = _local_completion_state()
    except (ValueError, TypeError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    if recovery is None or delivery is None:
        return JSONResponse({"ok": True, "pending": False})
    return JSONResponse({
        "ok": True,
        "pending": True,
        "request_id": recovery.request_id,
        "request": recovery.request.to_dict(),
        "invite": recovery.invite.to_dict(),
        "channel_binding": recovery.channel_binding,
        "approval": delivery.approval.to_dict(),
        "roster_entry": delivery.roster_entry.to_dict(),
        "origin_entry": (
            delivery.origin_entry.to_dict()
            if delivery.origin_entry is not None else None
        ),
    })


async def relay_probe(request: Request) -> JSONResponse:
    """Prove the relay carrier end to end from this machine (auto-fh2nv):
    the personal connector pairs with every other slot of the org the relay
    reports and runs the real fleet handshake. Body: optional ``targets``
    (list of {persona_pub, machine}) and ``timeout`` seconds."""
    denied = _operator_required(request)
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    try:
        reply = link_serving_supervisor.control(
            None, "fleet-relay-probe",
            {k: v for k, v in body.items() if k in ("targets", "timeout")},
            timeout=float(body.get("timeout") or 10.0) + 5.0,
        )
    except link_serving_supervisor.TunnelUnavailable as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)
    return JSONResponse(reply, status_code=200 if reply.get("ok") else 502)


async def fleet_status(request: Request) -> JSONResponse:
    """One decisive verdict for "is fleet-sync working, and if not, why" --
    the HTTP face of fleet_doctor's top line (tools/network/fleet_verdict.py).
    Same probes either way: an operator SSH'd into a box and a browser
    hitting this endpoint see the same answer, no log-grepping required."""
    denied = _operator_required(request)
    if denied is not None:
        return denied
    from tools.network.fleet_verdict import compute_verdict

    org = request.query_params.get("org")
    return JSONResponse(compute_verdict(org))


async def local_runtime_context(request: Request) -> JSONResponse:
    denied = _operator_required(request)
    if denied is not None:
        return denied
    try:
        context = _runtime_context()
    except (ValueError, TypeError, fleet_roster.FleetRosterError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    if context is None:
        return JSONResponse({"ok": True, "enabled": False})
    root_pub, entry = context
    binding = _reachability_binding()
    return JSONResponse({
        "ok": True,
        "enabled": True,
        "personal_root_pub": root_pub,
        "machine_id": entry.machine_id,
        "machine_pub": entry.machine_pub,
        # Present once the personal org is registered on auto.network. The
        # browser mints the reachability cert with org=org_uuid; null means
        # discovery stays off and only the sync credential is delivered.
        "org_uuid": binding.get("org_uuid") if binding else None,
        # The deterministic org_uuid the personal identity registers itself
        # under (the "personal tunnel"). Stable per personal root, so the browser
        # can register it at unlock — idempotently — whenever org_uuid is still
        # null, and every machine derives the same value.
        "personal_org_uuid": fleet_runtime.personal_org_uuid(root_pub),
        # Whether THIS machine may serve, and therefore whether the browser
        # mints it a personal serving delegate at unlock.
        #
        # This is the SECOND designation gate, and the one that actually strands
        # a machine. The supervisor's gate only decides whether to launch a
        # connector; this one decides whether the serving CERTIFICATE is ever
        # minted, and the key material for that mint exists only in the
        # operator's browser during unlock. A non-designated machine answered
        # False here forever, so it never acquired a personal serve-cert — and
        # relaxing the supervisor alone would leave it launching nothing,
        # because there is no credential to launch with. Observed on SJC
        # (fleet_doctor: personal serving cert missing, no local serve artifact).
        #
        # The joiner property the old expression protected is preserved by the
        # predicate itself, not by designation: a mid-join machine fails closed
        # (`fleet-member-provisioning`), as does one absent from the roster.
        "serves": fleet_tunnel_server.tunnel_serving_permitted()[0],
        # The orgs this machine serves, each with the ids the browser needs to
        # derive its per-org serving machine key. The root never leaves the
        # browser, so this list is the only way those keys can exist at all.
        "serving_orgs": serving_org_targets(),
    })


async def activate_local_runtime(request: Request) -> JSONResponse:
    denied = _operator_required(request)
    if denied is not None:
        return denied
    try:
        body = await request.json()
        credential = _activate_runtime(body)
    except (
        ValueError,
        TypeError,
        fleet_runtime.FleetRuntimeError,
        fleet_roster.FleetRosterError,
    ) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "machine_id": credential.machine_id})


async def complete_local_enrollment(request: Request) -> JSONResponse:
    denied = _operator_required(request)
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "body must be JSON"}, status_code=400)
    expected = {"request_id", "machine_id", "proof", "runtime"}
    if not isinstance(body, dict) or set(body) != expected:
        return JSONResponse(
            {"ok": False, "error": f"body must carry exactly {sorted(expected)}"},
            status_code=400,
        )
    try:
        state, recovery, delivery = _local_completion_state()
        if recovery is None or delivery is None:
            raise ValueError("no approved local fleet enrollment is awaiting completion")
        if body["request_id"] != recovery.request_id:
            raise ValueError("completion is for a different enrollment request")
        existing = machine_boot.machine_id(org="machine")
        if existing is None:
            machine_boot.accept_browser_completion(
                delivery,
                recovery.request,
                invite=recovery.invite,
                channel_binding=recovery.channel_binding,
                request_id=recovery.request_id,
                machine_id_value=body["machine_id"],
                proof=body["proof"],
                org="machine",
            )
        elif existing != body["machine_id"]:
            raise ValueError("this Dashboard already has a different Fleet identity")
        _activate_runtime(body["runtime"])
        state.delete(recovery.request_id)
    except (
        ValueError,
        TypeError,
        machine_boot.MachineBootError,
        fleet_runtime.FleetRuntimeError,
    ) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "machine_id": body["machine_id"]})


async def restore_fleet_serving(request: Request) -> JSONResponse:
    """Restart every serving connector — the server half of the tray's restart
    button (bead auto-sdrsa).

    A connector holds its Fleet serving credential ONLY in memory and keeps
    running whatever code it imported at launch, so a restart is what clears a
    stale-code process and is the precondition for re-arming an unarmed one: the
    browser re-mints the credential and POSTs it to /api/fleet/runtime once the
    fresh processes are back up, in that order (arming before the restart would
    just be thrown away). Operator-gated because it restarts real background
    processes. Never raises for one scope's failure — the others still restart."""
    denied = _operator_required(request)
    if denied is not None:
        return denied
    try:
        supervisor = link_serving_supervisor.get_supervisor()
        scopes = link_serving_supervisor._discover_startup_orgs()
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    restarted: list = []
    failed: list = []
    for scope in scopes:
        label = scope or "personal"
        try:
            # Only restart scopes actually set up to serve — a scope with no
            # cert row was never a serving process to begin with.
            if link_serving_supervisor.serve_cert_state(scope).get(
                "status"
            ) == "missing":
                continue
            result = supervisor.restart(scope)
            if (
                result.get("running") is not True
                or result.get("reason") == "owned-by-other-dashboard"
            ):
                failed.append({
                    "scope": label,
                    "error": str(result.get("reason") or "connector-not-running"),
                })
                continue
            restarted.append(label)
        except Exception as exc:  # noqa: BLE001 - one scope must not stall the rest
            failed.append({"scope": label, "error": str(exc)})
    return JSONResponse({"ok": True, "restarted": restarted, "failed": failed})


async def kick_machine(request: Request) -> JSONResponse:
    """Remove one machine from the fleet with a browser-signed tombstone.

    The browser mints the KICK RosterEntry under the personal root (fleet-
    kick.js); this route never sees a seed. It verifies the entry against the
    stored personal root, then refuses anything the operator should not be able
    to do from a single tap: kicking the machine they are on, kicking the
    machine currently serving the tunnel, kicking a machine that is not active,
    or a stale tombstone that does not beat the entry it revokes. Persisted via
    the same raw settings store enrollment commits to; roster-epoch consumers
    (the sync scheduler) drop the peer on their next re-resolve.
    """
    denied = _operator_required(request)
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "body must be JSON"}, status_code=400)
    if not isinstance(body, dict) or set(body) != {"roster_entry"}:
        return JSONResponse(
            {"ok": False, "error": "body must carry exactly roster_entry"},
            status_code=400,
        )
    try:
        entry = fleet_roster.RosterEntry.from_dict(body["roster_entry"])
        personal = identity_routes._personal_member()
        anchor = (personal.payload if personal is not None else {}).get("root_pub")
        if not isinstance(anchor, str) or not anchor:
            raise ValueError("no personal root is enrolled to anchor this fleet")
        fleet_roster.verify(entry, anchor_root_pub=anchor)
        if entry.kind != fleet_roster.EntryKind.KICK:
            raise ValueError("this route accepts only a kick tombstone")

        entries = tuple(fleet_roster.load_entries(org=None))
        active = fleet_roster.resolve(entries, anchor_root_pub=anchor)
        target = active.get(entry.machine_pub)
        if target is None:
            raise ValueError("target machine is not currently active in the fleet")
        # The tombstone must name the machine it revokes consistently — resolve
        # binds the true machine_id to the public key, and the local/serving
        # refusals below trust that id, not the entry's self-report.
        if entry.machine_id != target.machine_id:
            raise ValueError("kick machine_id does not match the active roster entry")
        if entry.seq <= target.seq:
            raise ValueError(
                "kick seq does not beat the target's current roster entry"
            )
        if target.machine_id == machine_boot.machine_id(org="machine"):
            raise ValueError("refusing to kick the machine you are using")
        if target.machine_id == fleet_tunnel_server.state().selected_machine_id:
            raise ValueError(
                "refusing to kick the machine currently serving the tunnel; "
                "select another server first"
            )
        fleet_roster.store_entry(entry, org=None)
    except (ValueError, TypeError, fleet_roster.FleetRosterError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({
        "ok": True,
        "machine_id": entry.machine_id,
        "entry_id": entry.entry_id,
    })


async def deactivate_invite(request: Request) -> JSONResponse:
    """Disable the current invitation locally so no new admission request can
    arrive. The caller separately posts a ``link_revoke`` approval to tear the
    published rendezvous route down; this endpoint owns only the local flag."""
    denied = _operator_required(request)
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "body must be JSON"}, status_code=400)
    if not isinstance(body, dict) or set(body) != {"target_uuid"} \
            or not isinstance(body.get("target_uuid"), str):
        return JSONResponse(
            {"ok": False, "error": "body must carry exactly ['target_uuid']"},
            status_code=400,
        )
    store = fleet_enrollment_service.FleetEnrollmentStore()
    deactivated = store.deactivate_invitation(body["target_uuid"])
    if not deactivated:
        return JSONResponse(
            {"ok": False, "error": "no active invitation matches that target"},
            status_code=404,
        )
    return JSONResponse({"ok": True})


async def reactivate_invite(request: Request) -> JSONResponse:
    """Honour a previously deactivated invitation again by flipping the SAME
    stored invite active. This never re-mints: the machine's in-flight join is
    pinned to this invitation's id, so only the original invite can readmit it.
    The rendezvous route is untouched here (deactivate's local flag is what the
    enrollment endpoints consult); a route torn down by ``link_revoke`` is a
    separate, terminal action and would be re-published on its own path."""
    denied = _operator_required(request)
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "body must be JSON"}, status_code=400)
    if not isinstance(body, dict) or set(body) != {"target_uuid"} \
            or not isinstance(body.get("target_uuid"), str):
        return JSONResponse(
            {"ok": False, "error": "body must carry exactly ['target_uuid']"},
            status_code=400,
        )
    store = fleet_enrollment_service.FleetEnrollmentStore()
    reactivated = store.reactivate_invitation(body["target_uuid"])
    if not reactivated:
        return JSONResponse(
            {"ok": False, "error": "no deactivated invitation matches that target"},
            status_code=404,
        )
    return JSONResponse({"ok": True})


ROUTES = [
    Route("/api/fleet/invitations/register", register_invite, methods=["POST"]),
    Route("/api/fleet/machines/kick", kick_machine, methods=["POST"]),
    Route(
        "/api/fleet/invitations/deactivate", deactivate_invite, methods=["POST"]
    ),
    Route(
        "/api/fleet/invitations/reactivate", reactivate_invite, methods=["POST"]
    ),
    Route("/api/fleet/enrollment/requests", pending_requests, methods=["GET"]),
    Route(
        "/api/fleet/enrollment/local-resume",
        resume_local_enrollment,
        methods=["POST"],
    ),
    Route(
        "/api/fleet/enrollment/local-completion",
        local_completion_context,
        methods=["GET"],
    ),
    Route(
        "/api/fleet/enrollment/local-completion",
        complete_local_enrollment,
        methods=["POST"],
    ),
    Route(
        "/api/fleet/enrollment/local-sync-status",
        local_sync_status,
        methods=["GET"],
    ),
    Route(
        "/api/fleet/runtime",
        local_runtime_context,
        methods=["GET"],
    ),
    Route(
        "/api/fleet/runtime",
        activate_local_runtime,
        methods=["POST"],
    ),
    Route("/api/fleet/status", fleet_status, methods=["GET"]),
    Route("/api/fleet/relay-probe", relay_probe, methods=["POST"]),
    Route("/api/fleet/restore", restore_fleet_serving, methods=["POST"]),
]
