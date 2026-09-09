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


def _own_standing_route_hint():
    """This machine's own standing relay route in the ws spelling the
    registry admits as a hint, or None when it has none to announce."""
    from tools.network import fleet_route

    try:
        own = fleet_route.load_self(org="machine")
    except Exception:
        return None
    if own is None:
        return None
    try:
        _http, ws_base, token = fleet_relay_sync._route_location(own.rendezvous)
    except fleet_relay_sync.FleetRelaySyncError:
        return None
    return f"{ws_base}/l/{token}"


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
    from tools.network import fleet_reachability

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
        relay_url=_own_standing_route_hint,
        # Off the event loop: a due refresh runs on its own thread and the
        # scheduler gets the last map at once (auto-8dw0w).
        background=True,
    )
    _reachability_cache = cache
    # The relay puller follows the origin's published standing route over the
    # stored bootstrap (invitation) route; discovery is this same cache.
    fleet_relay_sync.standing_route_resolver = (
        lambda origin_pub: cache.relay_routes().get(origin_pub)
    )

    def peers():
        merged = dict(cache.peers())
        merged.update(_fleet_env_peers())
        if not pull_direct:
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
    fleet_relay_sync.dashboard_relay_sync_service.configure(credential)
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
    return credential


def _local_sync_phase() -> str:
    """Return the compact first-sync phase from durable local evidence.

    A successful delta pull is not enough for a newly enrolled Dashboard: it
    may contain only writes made after catalog activation.  The onboarding
    completion boundary is therefore a validated checkpoint receipt from one
    active remote roster member in the current roster epoch.  Until that
    receipt exists the truthful state remains ``synchronizing``.
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
            "AND checkpoints_received>0 AND last_success_ns IS NOT NULL LIMIT 1",
            (epoch, *remote_keys),
        ).fetchone()
        return "complete" if row is not None else "synchronizing"
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
    Route("/api/fleet/restore", restore_fleet_serving, methods=["POST"]),
]
