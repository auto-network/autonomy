"""Truthful read model for the Fleet Machines surface.

The browser consumes one projection rather than independently joining the
personal roster, machine identity, tunnel assignment, sync observations,
invitation transport, and generic approval rendezvous.  Approval ids are an
internal correlation seam only; this module never exposes decision controls.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import sqlite3
from pathlib import Path
import time
from typing import Mapping

from tools.dashboard import link_operations
from tools.dashboard.fleet_enrollment_service import (
    FleetEnrollmentStore,
    PendingEnrollment,
    StoredFleetInvitation,
)
from tools.graph.db import _org_db_path
from tools.network import (
    fleet_invite,
    fleet_machine_profile,
    fleet_roster,
    fleet_sync_scheduler,
    fleet_counter_baseline,
    fleet_sync_peer_scope,
    fleet_sync_telemetry,
    fleet_sync_traffic,
    fleet_tunnel_server,
    machine_boot,
)


@dataclass(frozen=True)
class ProjectionInputs:
    server_time: int
    root_pub: str | None
    roster_entries: tuple[fleet_roster.RosterEntry, ...]
    local_machine_id: str | None
    selected_machine_id: str | None
    peer_rows: Mapping[str, Mapping]
    admissions: tuple[PendingEnrollment, ...]
    approvals: Mapping[str, str | None]
    invitation: StoredFleetInvitation | None
    deactivated_invitation: StoredFleetInvitation | None = None
    machine_names: Mapping[str, str] = field(default_factory=dict)
    invitation_publication: Mapping | None = None
    publishing_org: str = "personal"
    telemetry_rows: Mapping[str, Mapping] = field(default_factory=dict)
    #: {peer: [{scope, frontier_ns, measured, behind_ns, behind_origin,
    #: unreceived, observed_at_ns, bytes_in, bytes_out}]}
    peer_scope_rows: Mapping[str, list] = field(default_factory=dict)
    #: One row per transport/direction/scope, each carrying its two rings.
    traffic_rows: tuple = ()
    #: {scope: oldest origin position THIS machine holds}
    local_frontiers: Mapping[str, int] = field(default_factory=dict)
    #: {scope: {origin: this machine's cursor for that origin}} -- W_L[o]
    local_cursors: Mapping[str, Mapping[str, int]] = field(default_factory=dict)
    #: {scope: {origin: {ns, peer}}} -- best cursor any peer reported per origin
    best_known: Mapping[str, Mapping[str, dict]] = field(default_factory=dict)
    #: {scope: {phase, frontier}} for any scope mid-bootstrap
    bootstrap_states: Mapping[str, dict] = field(default_factory=dict)
    #: {(peer, scope): counters at the operator's last reset}
    counter_baselines: Mapping = field(default_factory=dict)
    local_verdict: Mapping | None = None
    serve_cert: Mapping | None = None
    tunnel_serving: bool | None = None
    #: Labels of the provisioned scopes that are NOT serving. Empty when
    #: tunnel_serving is True; naming them is what turns "something is wrong"
    #: into "anchore is wrong".
    tunnel_scopes_down: tuple = ()
    #: Per provisioned scope, the supervisor's facts (link_serving_supervisor.
    #: scope_states): {scope, label, state, launch_exits, cache_present}.
    #: ``state`` is serving / unarmed / launch-failing / down. This is what
    #: lets the card say "re-arm failed: personal" instead of "Needs unlock"
    #: when the vault is warm (graph://1418ca10-588 section 4).
    scope_states: tuple = ()
    #: Whether this machine's vaulted runtime credential can be opened in this
    #: process right now (graph://67d0aa5f-885 D3). With it present an unarmed
    #: connector is a re-arm failure, not a locked vault; absent means either
    #: no activation ever happened here or the vault is cold.
    dashboard_credential_present: bool | None = None


def _peer_rows(epoch: str | None) -> dict[str, dict]:
    """Per-peer observations from this Dashboard's personal database.

    Aggregated across EVERY roster epoch, not just the current one.
    fleet_sync_peer_state is keyed (machine_public_key, roster_epoch), so a
    roster change -- a machine joining, a kick -- starts a fresh row at zero
    for every peer. Reading only the current epoch made "Changes applied"
    restart while the byte totals beside it, which come from epoch-free
    telemetry, kept climbing. The screen showed 49 GB sent and 0 changes
    applied to the same peer, and the honest reading of that pair is that
    synchronization is broken, which it was not.

    Counters sum and point-in-time facts take their newest value. The
    scheduler still reads this table by exact epoch for its own decisions;
    this function is the display view and wants the machine's whole history
    with that peer, on the same footing as everything rendered next to it.
    """
    if epoch is None:
        return {}
    path = _org_db_path("personal")
    if not path.exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='fleet_sync_peer_state'"
        ).fetchone()
        if exists is None:
            return {}
        cols = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(fleet_sync_peer_state)")
        }
        built = ",peer_built_at" if "peer_built_at" in cols else ""
        # last_error_code is the one field that must come from the NEWEST
        # row rather than an aggregate: an error from a retired epoch is not
        # this peer's current state, and MAX() over text would pick by
        # alphabetical order, which means nothing.
        built_agg = ",MAX(peer_built_at) AS peer_built_at" if "peer_built_at" in cols else ""
        rows = conn.execute(
            "SELECT machine_public_key,"
            " MAX(last_success_ns) AS last_success_ns,"
            " SUM(bytes_sent) AS bytes_sent,"
            " SUM(bytes_received) AS bytes_received,"
            " SUM(transactions_applied) AS transactions_applied,"
            " SUM(retries) AS retries,"
            " MAX(peer_watermark) AS peer_watermark,"
            f" MAX(updated_at_ns) AS updated_at_ns{built_agg}"
            " FROM fleet_sync_peer_state GROUP BY machine_public_key"
        ).fetchall()
        current = {
            str(row["machine_public_key"]): row["last_error_code"]
            for row in conn.execute(
                "SELECT machine_public_key,last_error_code FROM "
                "fleet_sync_peer_state WHERE roster_epoch=?", (epoch,),
            )
        }
        out: dict[str, dict] = {}
        for row in rows:
            peer = str(row["machine_public_key"])
            record = dict(row)
            record["last_error_code"] = current.get(peer)
            out[peer] = record
        return out
    finally:
        if "conn" in locals():
            conn.close()


def _publication_from_journal(key: str, entry: Mapping) -> dict | None:
    """One recorded fleet:join publish from the link-operation journal
    (link_operations.Journal), in the shape the invitation view reads:
    ``request``, ``created_at`` in seconds, and ``result`` null while the
    operation is still running, else ``{approved, execution}``."""
    request = entry.get("request")
    if entry.get("op") != "publish" or not isinstance(request, Mapping):
        return None
    if request.get("target_type") != "fleet:join":
        return None
    state = entry.get("state")
    if state in ("prepared", "claimed"):
        result = None
    else:
        execution = entry.get("execution")
        if not isinstance(execution, Mapping):
            execution = {"ok": False, "error": "Invitation publication did not complete."}
        result = {"approved": True, "execution": dict(execution)}
    return {
        "id": key,
        "created_at": float(entry.get("prepared_at") or 0),
        "request": dict(request),
        "result": result,
    }


def _invitation_publication(journal_entries) -> dict | None:
    """The newest fleet:join publication this machine recorded. Every
    publish, the operator's own from the Fleet page and an approved Central
    request alike, runs through the link operation and is recorded in the
    journal (auto-fkhq0.10a); nothing else produces one."""
    candidates = [
        adapted for key, entry in journal_entries
        if (adapted := _publication_from_journal(key, entry)) is not None
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda row: float(row.get("created_at") or 0))


def _load_inputs(*, now_ms: int) -> ProjectionInputs:
    root_pub = fleet_tunnel_server._personal_root_pub()
    entries = tuple(fleet_roster.load_entries(org=None))
    if entries and root_pub is None:
        raise ValueError("Fleet roster exists but its personal root is unavailable")
    epoch = None
    if root_pub is not None:
        try:
            epoch = fleet_sync_scheduler.roster_epoch(entries, root_pub)
        except Exception:
            epoch = None
    local_machine_id = machine_boot.machine_id(org="machine")
    selected_machine_id = fleet_tunnel_server.state().selected_machine_id
    store = FleetEnrollmentStore()
    admissions = store.list_admissions()
    from tools.dashboard import fleet_enrollment_approvals
    approvals = {
        row.source_approval_id: fleet_enrollment_approvals.decision_status(row.source_approval_id)
        for row in admissions if row.source_approval_id
    }
    invitation_publication = _invitation_publication(link_operations.Journal.entries())
    # The local machine's operational facts (connector armed, code staleness,
    # serving certificate). Probes are best-effort: an unreadable probe leaves
    # the field null and the browser shows nothing rather than a guess.
    local_verdict = None
    try:
        from tools.network.fleet_verdict import compute_verdict

        local_verdict = compute_verdict(None)
    except Exception:
        local_verdict = None
    serve_cert = None
    try:
        from tools.dashboard.link_serving_supervisor import serve_cert_state

        serve_cert = serve_cert_state(None)
    except Exception:
        serve_cert = None
    # The same live handshake probe the profile panel's Tunnel indicator uses
    # (unlock-state): True only when the connector child reports a completed
    # tunnel handshake. None when the probe is unavailable.
    # ASK EVERY PROVISIONED SCOPE, not only personal. This card said
    # "Tunnel: Serving" on sjc-2 while three org connectors were dead, because
    # serving() with no argument means the personal scope — the identical
    # defect the profile tray's Tunnel tile had, in the second of its two
    # callers. Both now go through one helper so they cannot disagree.
    tunnel_serving = None
    tunnel_scopes_down: tuple = ()
    scope_states: tuple = ()
    try:
        from tools.dashboard.link_serving_supervisor import scope_states as _scope_states

        rows = _scope_states()
        scope_states = tuple(rows)
        down = [row["label"] for row in rows if not row.get("serving")]
        tunnel_scopes_down = tuple(down)
        tunnel_serving = not down
    except Exception:
        tunnel_serving = None
        tunnel_scopes_down = ()
        scope_states = ()
    dashboard_credential_present = None
    try:
        from tools.dashboard import fleet_enrollment_routes as _fer

        payload, _reason = _fer._load_runtime_credential()
        dashboard_credential_present = payload is not None
    except Exception:
        dashboard_credential_present = None
    return ProjectionInputs(
        server_time=now_ms,
        root_pub=root_pub,
        roster_entries=entries,
        local_machine_id=local_machine_id,
        selected_machine_id=selected_machine_id,
        peer_rows=_peer_rows(epoch),
        admissions=admissions,
        approvals=approvals,
        invitation=store.current_invitation(now_ms=now_ms),
        deactivated_invitation=store.deactivated_invitation(now_ms=now_ms),
        machine_names=fleet_machine_profile.names(org=None),
        invitation_publication=invitation_publication,
        publishing_org="personal",
        telemetry_rows=fleet_sync_telemetry.read_peer_totals(org="machine"),
        peer_scope_rows=fleet_sync_peer_scope.read_peer_scopes(org="machine"),
        traffic_rows=tuple(fleet_sync_traffic.read_traffic_rows(org="machine")),
        local_frontiers=_local_frontiers(),
        local_cursors=_local_cursors(),
        best_known=fleet_sync_peer_scope.read_best_known(org="machine"),
        bootstrap_states=_bootstrap_states(),
        counter_baselines=fleet_counter_baseline.read(org="machine"),
        local_verdict=local_verdict,
        serve_cert=serve_cert,
        tunnel_serving=tunnel_serving,
        tunnel_scopes_down=tunnel_scopes_down,
        scope_states=scope_states,
        dashboard_credential_present=dashboard_credential_present,
    )


def _dashboard_certificate() -> dict | None:
    """{daysRemaining, notAfter, expiring} for the served certificate, or
    None when there is none to read."""
    try:
        from tools.dashboard import tls_certificate

        facts = tls_certificate.read_certificate()
    except Exception:
        return None
    if facts is None:
        return None
    days = facts.days_remaining()
    return {
        "daysRemaining": round(days, 1),
        "notAfter": int(facts.not_after.timestamp() * 1000),
        "expiring": days <= tls_certificate.EXPIRY_WARNING_DAYS,
    }


def _display_id(machine_id: str) -> str:
    return f"{machine_id[:6]}…{machine_id[-6:]}"


def _milliseconds(value) -> int | None:
    if value is None:
        return None
    number = int(value)
    return number // 1_000_000 if number > 1_000_000_000_000_000 else number


def _local_frontiers() -> dict[str, int]:
    """``{scope: oldest newest-transaction position this machine holds}``.

    Used ONLY for a scope mid-bootstrap, to say how far the sweep still has to
    fill (its target F minus this). It is a scalar across origins, so it is
    NEVER compared with a peer's position to compute lag: that compares two
    different origins and measures write recency, not a gap (8ee69a22, the
    false 5.3 d on Home). Lag is per origin -- ``_origin_gaps`` over the
    stored watermark maps and ``_local_cursors`` (graph://6aa9bffc-ca9
    Record 3; pitfall graph://e6dba57c-f8b).
    """
    from tools.network.fleet_sync_scheduler import discover_org_sync_scopes

    paths = {"personal": _org_db_path("personal")}
    try:
        paths.update(discover_org_sync_scopes())
    except Exception:
        pass
    out: dict[str, int] = {}
    for scope, path in paths.items():
        try:
            if not Path(path).exists():
                continue
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except Exception:
            continue
        try:
            present = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='fleet_sync_transactions'"
            ).fetchone()
            if present is None:
                continue
            rows = conn.execute(
                "SELECT MAX(t.timestamp_ns) FROM fleet_sync_transactions t "
                "JOIN fleet_sync_origins o ON o.id=t.origin_id "
                "GROUP BY o.incarnation"
            ).fetchall()
            values = [int(row[0]) for row in rows if row[0]]
            if values:
                out[scope] = min(values)
        except Exception:
            continue
        finally:
            conn.close()
    return out


def _scope_db_paths() -> dict[str, Path]:
    from tools.network.fleet_sync_scheduler import discover_org_sync_scopes

    paths = {"personal": _org_db_path("personal")}
    try:
        paths.update(discover_org_sync_scopes())
    except Exception:
        pass
    return {scope: Path(path) for scope, path in paths.items()}


def _local_cursors(paths: Mapping[str, Path] | None = None) -> dict[str, dict[str, int]]:
    """``{scope: {origin: W_L[origin]}}``: this machine's cursor per origin.

    ``fleet_sync_origin_cursor`` only -- the same value this machine
    advertises (MutationCatalog.origin_watermarks) and the doctor prints as
    "cursor at MAX". No MAX fallback: every write path creates the table, so
    a scope without one reports nothing and its lag reads unknown
    (graph://6aa9bffc-ca9 Record 3). A small table, one row per origin, so a
    render never scans the transaction log.
    """
    out: dict[str, dict[str, int]] = {}
    for scope, path in (paths if paths is not None else _scope_db_paths()).items():
        if not Path(path).exists():
            continue
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
        except Exception:
            continue
        try:
            present = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='fleet_sync_origin_cursor'").fetchone()
            if present is None:
                continue
            out[scope] = {
                str(origin): int(ts) for origin, ts in conn.execute(
                    "SELECT o.incarnation, c.timestamp_ns FROM "
                    "fleet_sync_origin_cursor c JOIN fleet_sync_origins o "
                    "ON o.id=c.origin_id")
            }
        except Exception:
            continue
        finally:
            conn.close()
    return out


def _bootstrap_states() -> dict[str, dict]:
    """``{scope: {phase, frontier}}`` for any scope mid-bootstrap.

    A machine filling a scope KNOWS how much is left: F is the serving store's
    frontier captured at sweep start and persisted precisely because it is not
    derivable from the receiving database. Until the bootstrap completes the
    store deliberately advertises nothing, which is correct on the wire and
    renders as silence on a screen -- a machine that is working hard and
    knows exactly how far it has to go looks identical to an idle one.
    """
    from tools.network.fleet_sync.sweep_receive import read_bootstrap
    from tools.network.fleet_sync_scheduler import discover_org_sync_scopes

    paths = {"personal": _org_db_path("personal")}
    try:
        paths.update(discover_org_sync_scopes())
    except Exception:
        pass
    out: dict[str, dict] = {}
    for scope, path in paths.items():
        if not Path(path).exists():
            continue
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except Exception:
            continue
        try:
            state = read_bootstrap(conn)
            if state is not None and state.phase.value != "complete":
                out[scope] = {
                    "phase": state.phase.value,
                    "frontier": dict(state.frontier or {}),
                }
        except Exception:
            continue
        finally:
            conn.close()
    return out


def _origin_gaps(
    ahead: Mapping[str, int], held: Mapping[str, int],
) -> list[dict]:
    """Per origin, how far ``held`` trails ``ahead``: the SAME origin on both
    sides, never two machines' scalars (graph://6aa9bffc-ca9 Record 3; pitfall
    graph://e6dba57c-f8b). ``ahead`` and ``held`` are cursor maps
    {origin: timestamp_ns}; the result lists only origins where ``held`` is
    behind, with ``lag`` in ms, or None when ``held`` has nothing of that
    origin at all ("not yet received", never ``ahead - 0``)."""
    gaps: list[dict] = []
    for origin, position in (ahead or {}).items():
        position = int(position or 0)
        mine = int((held or {}).get(origin) or 0)
        if position > mine:
            gaps.append({
                "origin": origin,
                "lag": (position - mine) // 1_000_000 if mine else None,
            })
    return gaps


def _worst(gaps: list[dict]) -> int | None:
    """0 when nothing trails, None when any origin is not yet received."""
    if any(gap["lag"] is None for gap in gaps):
        return None
    return max((gap["lag"] for gap in gaps), default=0)


def _active_best_known(
    best_known: Mapping[str, Mapping[str, dict]] | None, active: set[str],
) -> dict[str, dict[str, dict]]:
    """Only positions vouched for by an ACTIVE roster machine count: a peer
    removed from the roster (or re-enrolled under a new key) no longer holds
    anything this machine can pull, so its reports must not read as lag."""
    return {
        scope: {origin: best for origin, best in (origins or {}).items()
                if isinstance(best, dict) and best.get("peer") in active}
        for scope, origins in (best_known or {}).items()
    }


def _local_scope_rows(
    peer_scope_rows: Mapping[str, list],
    local: Mapping[str, int],
    bootstrap: Mapping[str, dict],
    cursors: Mapping[str, Mapping[str, int]] | None = None,
    best_known: Mapping[str, Mapping[str, dict]] | None = None,
) -> list[dict]:
    """The local machine's own per-organization row.

    It has no peer row about itself -- Record 2 is keyed by PEER -- so without
    this the local card renders an empty table and "0 / 0", which reads as a
    fault rather than as the category error it is.

    Bytes are summed across peers for the scope, which IS this machine's total
    in and out for that organization, and is lifetime like the peer rows beside
    it.

    Lag is how far THIS machine L trails its peers, PER ORIGIN
    (graph://6aa9bffc-ca9 Record 3; terms graph://1155b8f4-8cf; pitfall
    graph://e6dba57c-f8b): max over origins o of ``best[o] - W_L[o]``, where
    best[o] is the highest cursor any peer has reported for o (folded from
    every pull's map on arrival) and W_L our own cursor. That catches data a
    peer holds from ANY origin -- including one that never syncs with us
    directly -- in O(origins), whatever the number of peers. ``behind`` names,
    per peer that reported a position we lack, its worst origin and when it
    was heard. Lag is None -- unknown, never "in sync" -- when no peer has
    reported for the scope, or when we hold nothing of an origin a peer
    holds; ``notMeasured`` names peers never heard from. Mid-bootstrap the
    sweep's remaining distance also counts; the larger wins.

    Never compare one machine's scalar with another's: two wrong versions
    shipped (a hard-set 0; min-vs-min in 8ee69a22).
    """
    totals: dict[str, dict] = {}
    for scopes in (peer_scope_rows or {}).values():
        for row in scopes or []:
            entry = totals.setdefault(
                row["scope"], {"bytesIn": 0, "bytesOut": 0}
            )
            entry["bytesIn"] += int(row.get("bytes_in") or 0)
            entry["bytesOut"] += int(row.get("bytes_out") or 0)
    cursors = cursors or {}
    out: list[dict] = []
    for scope in sorted(set(local) | set(totals) | set(bootstrap) | set(cursors) | set(best_known or {})):
        entry = totals.get(scope) or {"bytesIn": 0, "bytesOut": 0}
        filling = bootstrap.get(scope)
        held = cursors.get(scope) or {}
        best = (best_known or {}).get(scope) or {}
        reported = {peer: row for peer, scopes in (peer_scope_rows or {}).items()
                    for row in scopes or [] if row.get("scope") == scope}
        not_measured = sorted(peer for peer, row in reported.items()
                              if not int(row.get("frontier_ns") or 0))
        ahead = {origin: int(b.get("ns") or 0) for origin, b in best.items()}
        by_peer: dict[str, list[dict]] = {}
        for gap in _origin_gaps(ahead, held):
            by_peer.setdefault(str(best[gap["origin"]].get("peer")), []).append(gap)
        behind = []
        for peer, gaps in by_peer.items():
            worst = max(gaps, key=lambda g: (g["lag"] is None, g["lag"] or 0))
            observed = int((reported.get(peer) or {}).get("observed_at_ns") or 0)
            behind.append({
                "peer": peer, "origin": worst["origin"], "lag": _worst(gaps),
                "origins": len(gaps),
                "observedAt": observed // 1_000_000 if observed else None,
            })
        lag = _worst([g for gaps in by_peer.values() for g in gaps]) if best else None
        if filling:
            # now - F is what remains: F is where the sweep is filling TO.
            target = max(filling["frontier"].values(), default=0)
            ours_min = int(local.get(scope) or 0)
            fill_lag = max(0, (target - ours_min) // 1_000_000) if target else None
            lag = fill_lag if lag is None else (
                None if fill_lag is None else max(lag, fill_lag))
        out.append({
            "scope": scope,
            "lag": lag,
            "behind": sorted(behind, key=lambda b: -(b["lag"] or 0)),
            "notMeasured": sorted(not_measured),
            "bytesIn": entry["bytesIn"],
            "bytesOut": entry["bytesOut"],
            "filling": filling["phase"] if filling else None,
        })
    return out


def _scope_rows(
    rows: list | None, *, server_time: int,
    baselines: Mapping | None = None, peer: str | None = None,
) -> list[dict]:
    """One row per organization for one peer P, as the Fleet view reads them.

    Lag is how far P trailed THIS machine at its last pull, per origin
    (graph://6aa9bffc-ca9 Record 3; pitfall graph://e6dba57c-f8b): max over
    origins o of ``W_L[o] - W_P[o]``, reduced when P's map arrived
    (``behind_ns``). Never ``now - anything`` and never one scalar against
    another. None -- unknown -- when P has not pulled from us since this was
    recorded, or when P held nothing of an origin we hold.
    """
    out: list[dict] = []
    for row in rows or []:
        scope = row.get("scope")
        measured = bool(row.get("measured"))
        unreceived = int(row.get("unreceived") or 0)
        origin = str(row.get("behind_origin") or "")
        out.append({
            "scope": scope,
            "lag": (int(row.get("behind_ns") or 0) // 1_000_000
                    if measured and not unreceived else None),
            "behindOn": [origin] if origin else [],
            "unreceived": unreceived,
            "bytesIn": fleet_counter_baseline.since(
                row.get("bytes_in"),
                (baselines or {}).get((peer, scope)), "bytes_received"),
            "bytesOut": fleet_counter_baseline.since(
                row.get("bytes_out"),
                (baselines or {}).get((peer, scope)), "bytes_sent"),
            "filling": None,
        })
    return out


def _observation(
    peer: Mapping | None,
    telemetry: Mapping | None = None,
    scopes: list | None = None,
    baseline: Mapping | None = None,
) -> dict:
    peer = peer or {}
    telemetry = telemetry or {}
    has_telemetry = bool(telemetry.get("iterations"))
    return {
        "lastSuccessfulSyncAt": max(
            filter(
                lambda value: value is not None,
                (
                    _milliseconds(peer.get("last_success_ns")),
                    _milliseconds(telemetry.get("last_success_at_ns")),
                ),
            ),
            default=None,
        ),
        "transactionsApplied": fleet_counter_baseline.since(
            peer.get("transactions_applied"), baseline, "transactions_applied"),
        # Minus whatever the operator's last reset cleared. The counters
        # themselves are never mutated -- see fleet_sync_counters.
        "bytesSent": fleet_counter_baseline.since(
            telemetry.get("bytes_sent") if has_telemetry
            else peer.get("bytes_sent"), baseline, "bytes_sent"),
        "bytesReceived": fleet_counter_baseline.since(
            telemetry.get("bytes_received") if has_telemetry
            else peer.get("bytes_received"), baseline, "bytes_received"),
        # Per-transport bytes, so the fleet view can show how much of a
        # peer's traffic went over the direct listener versus the relay.
        # Empty when only the peer_state fallback is available: that table is
        # keyed by peer alone and has no transport dimension to report.
        "bytesByTransport": {
            transport: {
                "sent": int(counts.get("bytes_sent") or 0),
                "received": int(counts.get("bytes_received") or 0),
                "transactions": int(counts.get("transactions") or 0),
            }
            for transport, counts in sorted(
                (telemetry.get("by_transport") or {}).items()
            )
        },
        "retryCount": int(peer.get("retries") or 0),
        "lastErrorCode": peer.get("last_error_code"),
        # The peer's build (committer date), learned from a schema refusal, so a
        # version-mismatch ("Paused") row can name WHICH build the peer runs.
        "peerBuiltAt": peer.get("peer_built_at"),
        "syncIterations": int(telemetry.get("iterations") or 0),
        "successfulIterations": int(
            telemetry.get("successful_iterations") or 0
        ),
        "failedIterations": int(telemetry.get("failed_iterations") or 0),
        "totalSyncDurationMs": int(telemetry.get("total_duration_ms") or 0),
        "lastSyncDurationMs": int(telemetry.get("last_duration_ms") or 0),
        "mutationFrames": int(telemetry.get("mutation_frames") or 0),
        "transactionsTransferred": int(telemetry.get("transactions") or 0),
        "lastSyncOutcome": telemetry.get("last_outcome"),
        # The names the Fleet view actually reads. "attempts" rather than
        # "iterations" because the screen is about what this peer tried, and
        # a failed attempt is the fact an operator is looking for.
        "attemptsFailed": fleet_counter_baseline.since(
            telemetry.get("failed_iterations"), baseline, "attempts_failed"),
        "lastOutcome": telemetry.get("last_outcome"),
        # How far this peer's own promise reaches, per scope, and the bytes
        # exchanged for it. Lag is derived against this response's serverTime
        # and never stored -- storing it would age.
        "scopes": scopes or [],
        # The peer's receive progress from this machine's journal, distinct
        # from the frontier above: this is how far WE got with that peer.
        "peerWatermarkAt": _milliseconds(peer.get("peer_watermark")),
    }


def _machine_row(
    entry: fleet_roster.RosterEntry,
    *,
    standing: str,
    local_machine_id: str | None,
    selected_machine_id: str | None,
    peer: Mapping | None,
    telemetry: Mapping | None,
    display_name: str | None,
    scopes: list | None = None,
    baseline: Mapping | None = None,
) -> dict:
    local = entry.machine_id == local_machine_id
    return {
        "rowKind": "roster_machine",
        "sourceApprovalId": None,
        "entryId": entry.entry_id,
        "machineId": entry.machine_id,
        "machinePublicKey": entry.machine_pub,
        # The current per-machine sequence — the kick ceremony mints seq+1 so
        # its tombstone beats the entry it revokes (fleet-kick.js / the /kick
        # route both rely on this being the resolved winner's seq).
        "seq": entry.seq,
        "displayLabel": display_name or (
            "This dashboard" if local else "Untitled machine"
        ),
        "isLocalMachine": local,
        "isTunnelServer": entry.machine_id == selected_machine_id,
        "standing": standing,
        "assignment": entry.assignment,
        "standingChangedAt": entry.issued_at,
        "presence": "blocked" if standing == "revoked" else "unreported",
        **_observation(
            peer if standing == "authorized" else None,
            telemetry if standing == "authorized" else None,
            scopes if standing == "authorized" else None,
            baseline if standing == "authorized" else None,
        ),
        # The browser-root removal command is deliberately not invented by
        # this read-only slice.
        "canRemove": False,
    }


def _revoked_entries(
    entries: tuple[fleet_roster.RosterEntry, ...],
    *,
    root_pub: str,
    active_public_keys: set[str],
) -> list[fleet_roster.RosterEntry]:
    verified = [entry for entry in entries if entry.personal_root_pub == root_pub]
    by_machine: dict[str, list[fleet_roster.RosterEntry]] = {}
    for entry in verified:
        by_machine.setdefault(entry.machine_pub, []).append(entry)
    revoked = []
    for machine_pub, machine_entries in by_machine.items():
        if machine_pub in active_public_keys:
            continue
        cited = {
            entry.supersedes for entry in machine_entries
            if entry.kind == fleet_roster.EntryKind.ENROLL
            and entry.supersedes is not None
        }
        live_kicks = [
            entry for entry in machine_entries
            if entry.kind == fleet_roster.EntryKind.KICK
            and entry.entry_id not in cited
        ]
        if live_kicks:
            live_kicks.sort(key=lambda entry: (-entry.seq, entry.entry_id))
            revoked.append(live_kicks[0])
    return revoked


def _admission_standing(
    row: PendingEnrollment,
    *,
    approval: str | None,
) -> tuple[str, str | None] | None:
    if row.status == "failed":
        return "admission_failed", row.last_error_code or "admission_failed"
    if not row.source_approval_id:
        return "admission_failed", "approval_registration_missing"
    if row.status == "approving":
        return "admission_in_progress", None
    if approval is None:
        return "admission_failed", "approval_record_missing"
    if approval == "open":
        return "pending_approval", None
    if approval != "granted":
        return None
    return "admission_in_progress", None


def _admission_row(
    row: PendingEnrollment, standing: str, error_code: str | None
) -> dict:
    return {
        "rowKind": "pending_admission",
        "sourceApprovalId": row.source_approval_id,
        "entryId": None,
        "machineId": None,
        "machinePublicKey": None,
        "displayLabel": "New machine",
        "isLocalMachine": False,
        "isTunnelServer": False,
        "standing": standing,
        "assignment": None,
        "standingChangedAt": row.updated_at,
        "presence": "not_applicable",
        "lastSuccessfulSyncAt": None,
        "transactionsApplied": 0,
        "bytesSent": 0,
        "bytesReceived": 0,
        "retryCount": 0,
        "lastErrorCode": error_code,
        "syncIterations": 0,
        "successfulIterations": 0,
        "failedIterations": 0,
        "totalSyncDurationMs": 0,
        "lastSyncDurationMs": 0,
        "mutationFrames": 0,
        "transactionsTransferred": 0,
        "lastSyncOutcome": None,
        "canRemove": False,
    }


def _organizations(traffic_rows, machines) -> list[dict]:
    """Visual identity for every scope this view mentions.

    The by-organization table renders a name and an icon per row, so a scope
    that appears in the data and not here would render as a blank label. The
    set is taken from the data itself rather than from the org directory, so
    the two can never disagree: every scope shown has an identity, and no
    identity is shipped for a scope nothing references.
    """
    from tools.dashboard.org_identity import resolve_org_identity

    slugs = {row.get("scope") for row in traffic_rows}
    for machine in machines:
        slugs.update(scope.get("scope") for scope in machine.get("scopes") or [])
    return [
        resolve_org_identity(slug)
        for slug in sorted(slug for slug in slugs if slug)
    ]


def project(inputs: ProjectionInputs) -> dict:
    active: dict[str, fleet_roster.RosterEntry] = {}
    if inputs.root_pub is not None:
        active = fleet_roster.resolve(
            inputs.roster_entries, anchor_root_pub=inputs.root_pub
        )
    roster_rows = [
        _machine_row(
            entry,
            standing="authorized",
            local_machine_id=inputs.local_machine_id,
            selected_machine_id=inputs.selected_machine_id,
            peer=inputs.peer_rows.get(machine_pub),
            telemetry=inputs.telemetry_rows.get(machine_pub),
            display_name=inputs.machine_names.get(entry.machine_id),
            baseline=inputs.counter_baselines.get(
                (machine_pub, fleet_counter_baseline.MACHINE_SCOPE)),
            scopes=(
                _local_scope_rows(
                    inputs.peer_scope_rows, inputs.local_frontiers,
                    inputs.bootstrap_states, inputs.local_cursors,
                    _active_best_known(inputs.best_known, set(active)),
                )
                if entry.machine_id == inputs.local_machine_id
                else _scope_rows(
                    inputs.peer_scope_rows.get(machine_pub),
                    server_time=inputs.server_time,
                    baselines=inputs.counter_baselines,
                    peer=machine_pub,
                )
            ),
        )
        for machine_pub, entry in active.items()
    ]
    roster_rows.sort(
        key=lambda row: (not row["isLocalMachine"], row["machineId"] or "")
    )
    revoked_rows = []
    if inputs.root_pub is not None:
        revoked_rows = [
            _machine_row(
                entry,
                standing="revoked",
                local_machine_id=inputs.local_machine_id,
                selected_machine_id=inputs.selected_machine_id,
                peer=None,
                telemetry=None,
                display_name=inputs.machine_names.get(entry.machine_id),
            )
            for entry in _revoked_entries(
                inputs.roster_entries,
                root_pub=inputs.root_pub,
                active_public_keys=set(active),
            )
        ]
        revoked_rows.sort(key=lambda row: row["machineId"] or "")

    admission_rows = []
    for row in inputs.admissions:
        approval_id = row.source_approval_id
        lifecycle = _admission_standing(
            row,
            approval=inputs.approvals.get(approval_id) if approval_id else None,
        )
        if lifecycle is None:
            continue
        admission_rows.append(_admission_row(row, *lifecycle))

    observations = [inputs.peer_rows.get(machine_pub) or {} for machine_pub in active]
    last_successes = [
        _milliseconds(row.get("last_success_ns")) for row in observations
        if row.get("last_success_ns") is not None
    ]
    invitation = inputs.invitation
    invitation_view = {
        "status": "none",
        "url": None,
        "publishedAt": None,
        "expiresAt": None,
        "publishingOrg": inputs.publishing_org,
        "error": None,
    }
    if invitation is not None:
        invitation_view = {
            "status": "active",
            "targetUuid": invitation.target_uuid,
            "url": invitation.invite.rendezvous,
            "bootstrapCode": "AUTONOMY_FLEET_INVITE=" + fleet_invite.encode(invitation.invite),
            "publishedAt": invitation.created_at,
            "expiresAt": invitation.invite.expires_at or None,
            "publishingOrg": inputs.publishing_org,
            "error": None,
        }
    elif inputs.deactivated_invitation is not None:
        # Signed but deactivated. The route stands and the invite is intact;
        # only the active flag is off. Offer Reactivate (flip THIS invite back
        # on) rather than the fall-through 'awaiting_signature', which would
        # re-mint and strand any machine pinned to this invitation.
        deactivated = inputs.deactivated_invitation
        invitation_view = {
            "status": "inactive",
            "targetUuid": deactivated.target_uuid,
            "url": deactivated.invite.rendezvous,
            "bootstrapCode": "AUTONOMY_FLEET_INVITE=" + fleet_invite.encode(deactivated.invite),
            "publishedAt": deactivated.created_at,
            "expiresAt": deactivated.invite.expires_at or None,
            "publishingOrg": inputs.publishing_org,
            "error": None,
        }
    elif inputs.invitation_publication is not None:
        publication = inputs.invitation_publication
        request = publication.get("request") or {}
        result = publication.get("result")
        created_at = int(float(publication.get("created_at") or 0) * 1000)
        ttl = (request.get("meta") or {}).get("ttl")
        if isinstance(result, Mapping) and "ttl" in result:
            ttl = result.get("ttl")
        expires_at = created_at + ttl * 1000 if type(ttl) is int else None
        common = {
            "url": None,
            "publishedAt": created_at or None,
            "expiresAt": expires_at,
            "publishingOrg": request.get("org") or inputs.publishing_org,
            "targetUuid": request.get("target_uuid"),
            "error": None,
        }
        if result is None:
            invitation_view = {**common, "status": "publishing"}
        elif result.get("approved") is not True:
            invitation_view = {
                **common,
                "status": "failed",
                "error": "Invitation publication was declined.",
            }
        else:
            execution = result.get("execution")
            if isinstance(execution, Mapping) and execution.get("ok") is True:
                invitation_view = {
                    **common,
                    "status": "awaiting_signature",
                    "rendezvous": execution.get("url"),
                    "grantToken": execution.get("token"),
                }
            else:
                invitation_view = {
                    **common,
                    "status": "failed",
                    "error": (
                        execution.get("error")
                        if isinstance(execution, Mapping)
                        else "Invitation publication did not complete."
                    ),
                }
    verdict = inputs.local_verdict or {}
    credential = verdict.get("credential") or {}
    cert = inputs.serve_cert or {}
    cert_not_after = cert.get("not_after")
    local_machine = {
        # False only when the probe positively reported an unconfigured
        # credential; None (probe unavailable) renders nothing.
        "connectorArmed": (
            None if credential.get("configured") is None
            else bool(credential.get("configured"))
        ),
        "runningStale": (
            (verdict.get("connector_version") or {}).get("status") == "stale"
            or (verdict.get("dashboard_version") or {}).get("status") == "stale"
        ) if verdict else None,
        "certStatus": cert.get("status"),
        "certValidUntil": (
            cert_not_after * 1000 if isinstance(cert_not_after, int) else None
        ),
        # The certificate this dashboard SERVES (not the serving delegate):
        # its expiry is a machine fact the operator must see before the
        # monthly renewal's failures matter (auto-1ei8m).
        "dashboardCertificate": _dashboard_certificate(),
        "tunnelServing": inputs.tunnel_serving,
        "tunnelScopesDown": list(inputs.tunnel_scopes_down),
        "scopeStates": [
            {
                "scope": row.get("scope"), "label": row.get("label"),
                "state": row.get("state"), "serving": bool(row.get("serving")),
                "launchExits": row.get("launch_exits"),
                "cachePresent": row.get("cache_present"),
            }
            for row in inputs.scope_states
        ],
        "dashboardCredentialPresent": inputs.dashboard_credential_present,
        "verdictTopLine": verdict.get("top_line"),
    }
    from tools.network import build_version
    return {
        "serverTime": inputs.server_time,
        "localMachine": local_machine,
        # This machine's build (committer date), so a version-mismatch row can
        # read "this build … / peer's build …" instead of an opaque digest.
        "localBuiltAt": build_version.disk_built_at(),
        "summary": {
            "authorizedMachines": len(active),
            "connectedMachines": None,
            "lastSuccessfulSyncAt": max(last_successes) if last_successes else None,
            "joinRequests": len(admission_rows),
        },
        "machines": [*roster_rows, *admission_rows, *revoked_rows],
        # The rate rings, verbatim. Slot selection and staleness are the
        # reader's job on both sides: the browser counts a slot only when its
        # stamp equals the epoch it is drawing, so a row written by an older
        # build cannot present stale slots as live.
        "trafficHistory": [dict(row) for row in inputs.traffic_rows],
        "organizations": _organizations(inputs.traffic_rows, roster_rows),
        "invitation": invitation_view,
        "activity": {
            "transactionsApplied": sum(row["transactionsApplied"] for row in roster_rows),
            "bytesSent": sum(row["bytesSent"] for row in roster_rows),
            "bytesReceived": sum(row["bytesReceived"] for row in roster_rows),
            "syncIterations": sum(row["syncIterations"] for row in roster_rows),
            "successfulIterations": sum(
                row["successfulIterations"] for row in roster_rows
            ),
            "failedIterations": sum(row["failedIterations"] for row in roster_rows),
            "totalSyncDurationMs": sum(
                row["totalSyncDurationMs"] for row in roster_rows
            ),
            "mutationFrames": sum(row["mutationFrames"] for row in roster_rows),
            "scope": "this_dashboard_current_roster",
        },
    }


def build_view(*, now_ms: int | None = None) -> dict:
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    return project(_load_inputs(now_ms=now))
