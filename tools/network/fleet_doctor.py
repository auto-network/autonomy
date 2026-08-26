"""Fleet diagnostic report — what state is this node actually in, right now.

Built 2026-08-22 during the SJC acceptance demo, after repeatedly having to
answer "did it sync / does it know who it is / is serving actually up" by
hand: SSH in, grep raw logs, run one-off scripts, and re-derive facts that
were already computed somewhere in the real code. This script collects those
same facts through the SAME functions the dashboard itself uses (never a
re-guess of the logic), so its answer matches what the live system believes.

Run it:
    .venv/bin/python3 -m tools.network.fleet_doctor
    .venv/bin/python3 -m tools.network.fleet_doctor --json

Works on the host and inside a node container identically — every path goes
through tools.data_paths / tools.graph.db's resolvers, the same ones the
dashboard process itself uses, so this never hardcodes a host-only path.

KNOWN LIMITATION, stated up front rather than faked: some state (whether
ConnectorFleetRuntime.scheduler is actually configured inside a *running*
connector subprocess) lives only in that subprocess's memory. A fresh
process like this one cannot see it directly -- this script infers serving
health from external, durable evidence instead (is the subprocess alive,
what does its own log say, does the org it's serving match the org that
SHOULD be serving) and says so plainly rather than guessing.

SCOPE, operator directive (2026-08-25): this is a diagnostic tool, not a
regression suite. Add a check here when an ERROR STATE has real odds of
recurring in production -- a class of failure this file's own checks would
have caught faster than manual log-grepping (the deprecate-orphans-its-own-
catalog-address bug this same night is the model case: a real trigger/
resolver mismatch, not a one-off). Do NOT add a check just because a bug was
found and fixed once with no plausible recurrence path -- that belongs in a
regression test near the code it protects, not here. Every check in this
file should answer a question an operator or another agent might still need
to ask tomorrow.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


_QUIET = False  # set True in main() for --json: report data still collected, nothing printed but the JSON


def _section(title: str) -> None:
    if not _QUIET:
        print(f"\n== {title} ==")


def _line(label: str, value, *, warn: bool = False, fail: bool = False) -> None:
    if _QUIET:
        return
    mark = "FAIL" if fail else ("WARN" if warn else "ok  ")
    print(f"  [{mark}] {label}: {value}")


def _detail(text: str) -> None:
    if not _QUIET:
        print(text)


# ── decisive verdict (read this first) ──────────────────────────────────

def check_verdict(report: dict, *, org: str | None = None) -> None:
    """The one line to read before any of the sections below. Same probes,
    same answer, as GET /api/fleet/status -- see fleet_verdict.py. Built
    2026-08-23 specifically so "is it working, and if not why" stops
    requiring an SSH-in-and-grep cycle; run this first, always."""
    from tools.network.fleet_verdict import compute_verdict

    verdict = compute_verdict(org)
    report["verdict"] = verdict
    if _QUIET:
        return
    top = verdict["top_line"]
    print(f"\nVERDICT: {top}")
    cv, dv = verdict["connector_version"], verdict["dashboard_version"]
    if cv.get("status") == "stale":
        print(f"  connector is running {cv.get('process_commit')}, disk has {cv.get('disk_commit')} -- reload it")
    if dv.get("status") == "stale":
        print(f"  this dashboard process is running {dv.get('process_commit')}, disk has {dv.get('disk_commit')} -- reload it")
    lp = verdict["last_pull"]
    if lp.get("outcome") == "failed":
        print(f"  last pull: {lp.get('reason')} -- {lp.get('detail', '')}")
    elif lp.get("outcome") == "success":
        print("  last pull: succeeded")
    counts = verdict["data"].get("counts")
    if counts:
        print(f"  local data: {counts}")


# ── identity + designation ──────────────────────────────────────────────

def _machine_identity_cross_check() -> tuple[str | None, list[str]]:
    """Read the machine-identity row directly off disk from every path the
    real resolver could mean, rather than trusting only the winner it picks.

    Derives the two candidate paths the SAME way tools.graph.db._org_db_path
    does (via _orgs_dir()), so this works unmodified on the host and inside
    a container with a differently-nested data/orgs layout -- never a
    hardcoded DATA_ROOT-relative guess.

    Found live 2026-08-22: resolve_local_store_path() (tools.data_paths,
    what _org_db_path("machine") delegates to) picks between the local
    store's new home (orgs_dir.parent/"machine.db") and its legacy home
    (orgs_dir/"machine.db") purely by whether the NEW-HOME FILE EXISTS --
    not whether the row being looked up is actually in it. The new home
    here is a large, actively-written file (schema registry, harness
    bootstrap, ...) -- migration is clearly underway -- but the
    machine-identity row specifically, written once at enrollment and never
    touched again, was never copied over, so it's still only in the legacy
    file. Once ANYTHING writes to the new home the resolver treats the
    whole local store as relocated and stops looking at the legacy file at
    all, silently orphaning every row that hasn't itself been rewritten
    since. This is a partial-migration bug, not a wrong-path one -- report
    both files' contents so it's visible instead of silently trusting
    whichever the resolver happens to pick.
    """
    import sqlite3
    from tools.graph.db import _orgs_dir

    orgs_dir = _orgs_dir()
    candidates = [orgs_dir.parent / "machine.db", orgs_dir / "machine.db"]
    found_in: list[str] = []
    machine_id = None
    for path in candidates:
        if not path.exists():
            continue
        try:
            conn = sqlite3.connect(str(path))
            row = conn.execute(
                "SELECT payload FROM settings WHERE set_id='autonomy.machine.identity'"
            ).fetchone()
            if row:
                found_in.append(str(path))
                machine_id = json.loads(row[0]).get("machine_id", machine_id)
        except sqlite3.OperationalError:
            continue
    return machine_id, found_in


def check_identity(report: dict) -> None:
    _section("Identity & tunnel designation")
    try:
        from tools.network import machine_boot, fleet_tunnel_server
        from tools.graph.db import _org_db_path

        local_id = machine_boot.machine_id(org="machine")
        disk_id, disk_paths = _machine_identity_cross_check()
        resolver_path = str(_org_db_path("machine"))
        if local_id is None and disk_id is not None:
            _line(
                "machine_boot.machine_id() vs direct disk read",
                f"PARTIAL MIGRATION -- the local store's resolved home "
                f"({resolver_path}) exists and is actively written (schema "
                "registry, harness bootstrap, ...), so the resolver treats "
                "the machine local store as fully relocated there -- but "
                "the machine-identity row itself, written once at "
                "enrollment and never touched since, was never copied over "
                f"and is still only on disk at {disk_paths}. Any process "
                "asking through the normal resolver gets nothing; the live "
                "dashboard likely still answers correctly only because it "
                "cached machine_id in memory back when it WAS reachable. "
                f"Using the disk value ({disk_id[:16]}...) for the rest of "
                "this report.",
                warn=True,
            )
            local_id = disk_id
        elif disk_id is not None and disk_id != local_id:
            _line(
                "machine_boot.machine_id() vs direct disk read",
                f"DISAGREE -- resolver says {local_id!r}, disk says {disk_id!r}",
                fail=True,
            )
        root_pub = fleet_tunnel_server._personal_root_pub()
        state = fleet_tunnel_server.state()
        report["local_machine_id"] = local_id
        report["personal_root_pub"] = root_pub
        report["tunnel_state"] = {
            "allowed": state.allowed,
            "managed": state.managed,
            "reason": state.reason,
            "selected_machine_id": state.selected_machine_id,
            "local_machine_id": state.local_machine_id,
            "active_machine_count": state.active_machine_count,
        }
        _line("local machine_id", local_id or "(none -- not yet enrolled)",
              warn=local_id is None)
        _line("personal root pub", root_pub or "(none -- vault cold or unenrolled)",
              warn=root_pub is None)
        is_selected = (
            state.selected_machine_id is not None
            and state.selected_machine_id == local_id
        )
        _line(
            "tunnel-server state",
            f"allowed={state.allowed} managed={state.managed} "
            f"reason={state.reason!r} active_machines={state.active_machine_count}",
            warn=not state.allowed,
        )
        _line(
            "this machine is the selected server",
            is_selected,
            warn=not is_selected and state.managed,
        )
        report["is_selected_server"] = is_selected
    except Exception as exc:
        _line("identity check", f"FAILED to run: {exc!r}", fail=True)
        report["identity_error"] = repr(exc)


def check_local_store_migration(report: dict) -> None:
    """The 'machine'/'personal' local stores may live at either the new
    home (data/<name>.db) or the legacy home (data/orgs/<name>.db); the
    resolver falls back to whichever it finds. But it decides "has this
    store been migrated?" purely by whether the new-home FILE EXISTS, not
    whether it's actually complete -- so a new home created by ordinary
    use (any write at all) permanently shadows a legacy file that still
    holds real, un-copied rows.

    Caught live 2026-08-22: machine.db's new home existed, and 194 rows
    -- including a live credential reference -- were stranded in the old
    file and invisible to every reader through the normal resolver. This
    check catches that class of bug directly: whenever BOTH homes exist
    for a local store, diff every settings row between them instead of
    trusting either one blindly.
    """
    _section("Local store migration completeness")
    try:
        from tools.graph.db import _orgs_dir
        import sqlite3

        orgs_dir = _orgs_dir()
        for name in ("machine", "personal"):
            target = orgs_dir.parent / f"{name}.db"
            legacy = orgs_dir / f"{name}.db"
            if not (target.exists() and legacy.exists()):
                continue  # single copy, either location -- nothing to compare

            def _keys(path: Path) -> set[tuple[str, str]]:
                conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                try:
                    return {
                        (r[0], r[1])
                        for r in conn.execute("SELECT set_id, key FROM settings").fetchall()
                    }
                except sqlite3.OperationalError:
                    return set()

            legacy_keys, target_keys = _keys(legacy), _keys(target)
            stranded = legacy_keys - target_keys
            report[f"{name}_local_store_stranded_rows"] = len(stranded)
            if stranded:
                _line(
                    f"{name}: rows stranded in legacy, invisible to the resolver",
                    f"{len(stranded)} row(s) exist only at {legacy}, e.g. "
                    f"{sorted(stranded)[:3]}. The resolver prefers {target} purely "
                    "because it exists, not because it's complete. Merge before "
                    "removing the legacy file -- a plain 'mv' per the DEPLOY.md "
                    "runbook would clobber the target's own content instead.",
                    fail=True,
                )
            else:
                _line(
                    f"{name}: legacy/new content",
                    "identical -- legacy file is redundant and safe to remove",
                )
    except Exception as exc:
        _line("local-store migration check", f"FAILED to run: {exc!r}", fail=True)


# ── roster ───────────────────────────────────────────────────────────────

def _roster_entry_setting_id(machine_pub: str) -> str | None:
    """The Settings row id backing one roster entry -- what
    --kick-roster-entry actually needs to target a removal. The row's
    KEY is a hash of the entry's content, not machine_pub itself, so
    this matches by scanning each row's payload directly."""
    import sqlite3
    from tools.graph.db import _org_db_path

    conn = sqlite3.connect(_org_db_path("personal"))
    rows = conn.execute(
        "SELECT id, payload FROM settings WHERE set_id='autonomy.fleet.roster'"
    ).fetchall()
    for setting_id, payload in rows:
        if machine_pub in payload:
            return setting_id
    return None


def check_roster(report: dict) -> None:
    _section("Fleet roster (personal.db, org=None)")
    try:
        from tools.network import fleet_roster

        entries = tuple(fleet_roster.load_entries(org=None))
        root_pub = report.get("personal_root_pub")
        active = (
            fleet_roster.resolve(entries, anchor_root_pub=root_pub)
            if root_pub else {}
        )
        report["roster_raw_entry_count"] = len(entries)
        report["roster_active_count"] = len(active)
        _line("raw entries stored", len(entries))
        _line("active (resolved) members", len(active))
        # active is keyed by machine_pub (fleet_roster.resolve's own contract),
        # NOT machine_id -- compare against entry.machine_id, not the dict key.
        report["roster_entries"] = []
        for machine_pub, entry in sorted(active.items()):
            marker = " <- this machine" if entry.machine_id == report.get("local_machine_id") else ""
            entry_id = _roster_entry_setting_id(machine_pub)
            report["roster_entries"].append({
                "machine_id": entry.machine_id, "machine_pub": machine_pub,
                "setting_id": entry_id, "issued_at": entry.issued_at,
            })
            _detail(
                f"        machine_id={entry.machine_id[:16]}...  seq={entry.seq}  "
                f"kick-with: --kick-roster-entry {entry_id}{marker}"
            )
        if len(entries) != len(active):
            _line(
                "stale/kicked entries present",
                f"{len(entries) - len(active)} raw entr{'y' if len(entries)-len(active)==1 else 'ies'} "
                "not in the active set (dead tombstones or superseded -- harmless unless you expect them gone)",
                warn=True,
            )
    except Exception as exc:
        _line("roster check", f"FAILED to run: {exc!r}", fail=True)
        report["roster_error"] = repr(exc)


# ── connector subprocesses ──────────────────────────────────────────────

def _running_connectors() -> dict[str, dict]:
    """org_uuid -> {pid, graph_org, started} for every live link_serving connector."""
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid,args"], capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:
        return {}
    found = {}
    for raw_line in out.splitlines():
        if "tools.dashboard.link_serving" not in raw_line or "--org" not in raw_line:
            continue
        m_pid = re.match(r"\s*(\d+)", raw_line)
        m_org = re.search(r"--org\s+(\S+)", raw_line)
        m_graph_org = re.search(r"--graph-org\s+(\S+)", raw_line)
        if not (m_pid and m_org):
            continue
        found[m_org.group(1)] = {
            "pid": int(m_pid.group(1)),
            "graph_org": m_graph_org.group(1) if m_graph_org else None,
        }
    return found


def check_serving_readiness(report: dict) -> None:
    """Should each scope's connector even be running -- not just 'is one
    running right now'. check_connectors only ever sees what already
    exists; a scope that has NEVER been provisioned (no serve-cert, no
    binding) looks identical to one that's merely down, unless something
    checks readiness directly.

    Caught live 2026-08-22/23: the personal/scopeless sync scope (org=None)
    -- the one SJC's fleet_relay_sync actually needs -- had zero
    autonomy.network.serve-cert rows and zero autonomy.network.binding
    rows in personal.db. Nothing was ever provisioned for it, so every
    connector-status check kept reporting on the wrong scopes (autonomy,
    dynbench) while the one that mattered was silently never attempted.

    Deliberately does NOT call link_serving_supervisor.ensure() -- that
    function has real side effects (it can stop a live connector as part
    of reconciling it), which a diagnostic must never risk. This only
    calls the read-only half: serve_cert_state() and the live-grant
    check it gates on.
    """
    _section("Serving readiness per scope (should this be running at all)")
    try:
        import time
        from tools.dashboard import link_serving_supervisor as sup
        from tools.graph import org_ops

        running = report.get("running_connectors", {})
        now = time.time()
        scopes: list[tuple[str, str | None]] = [("personal (scopeless)", None)]
        try:
            scopes.extend((ref.slug, ref.slug) for ref in org_ops.list_orgs())
        except Exception:
            pass

        for label, org in scopes:
            try:
                cert_state = sup.serve_cert_state(org, now=now)
                has_grant = sup._has_live_grant(org, now)
            except Exception as exc:
                _line(f"{label}: readiness check", f"FAILED to run: {exc!r}", fail=True)
                continue
            should_run = cert_state["status"] == "ok" and has_grant
            # org=None (settings_ops' "explicit scopeless write") and
            # org="personal" resolve to the exact same underlying database
            # (settings_ops(org=None) deterministically opens the personal
            # org DB -- see commit 669cf592), so a connector launched under
            # either name satisfies the other; they are not two independent
            # scopes that both need their own running process. Symmetric on
            # purpose -- an earlier one-directional version (None matches
            # "personal", but not the reverse) produced a live false
            # positive on the "personal" scope once the connector actually
            # started reporting --graph-org as unset (None) rather than the
            # literal string "personal".
            personal_aliases = {None, "personal"}
            aliases = personal_aliases if org in personal_aliases else {org}
            is_running = any(info.get("graph_org") in aliases for info in running.values())
            if should_run and not is_running:
                _line(
                    f"{label}: expected to be serving but isn't",
                    f"cert={cert_state['status']} live_grant={has_grant} -- "
                    "eligible per its own credentials, but no matching connector "
                    "process is up; check the watchdog / restart timing",
                    fail=True,
                )
            elif not should_run:
                _line(
                    f"{label}: not eligible to serve",
                    f"cert={cert_state['status']} live_grant={has_grant}"
                    + ("" if cert_state["status"] == "ok" else " -- never provisioned (no serve-cert)"),
                    warn=(cert_state["status"] != "ok"),
                )
            else:
                # Confirmed live 2026-08-23: "eligible and running" alone
                # was not enough. A connector can be up, have a real tunnel,
                # AND its serve-cert/grant checked out fine, while its
                # in-memory fleet-runtime scheduler is still never
                # configured (only a genuine unlock's POST /api/fleet/runtime
                # sets it, and it does not survive the connector's own
                # restart). That state is invisible from anything checked
                # above -- it needed a direct, live query against the exact
                # running process (connector-status) to actually confirm.
                configured_note = ""
                try:
                    status = sup.control(org, "connector-status", {})
                    configured = status.get("fleet_runtime_configured")
                    if configured is False:
                        _line(
                            f"{label}: serving readiness",
                            "connector is up and eligible, but "
                            "fleet_runtime_configured=False -- every sync pull will "
                            "refuse with 'serving machine is locked for Fleet sync' "
                            "until a genuine unlock configures it (and that "
                            "configuration will not survive this connector's next "
                            "restart)",
                            fail=True,
                        )
                        continue
                    elif configured is True:
                        configured_note = ", fleet_runtime_configured=True"
                except Exception:
                    pass  # connector-status is itself best-effort here
                _line(f"{label}: serving readiness", f"eligible and running{configured_note}")
    except Exception as exc:
        _line("serving readiness check", f"FAILED to run: {exc!r}", fail=True)


def check_connectors(report: dict) -> None:
    _section("Serving connector subprocesses")
    connectors = _running_connectors()
    report["running_connectors"] = connectors
    if not connectors:
        _line("connector subprocesses", "NONE running", warn=True)
    for org_uuid, info in connectors.items():
        _line(
            f"connector for org {org_uuid[:8]}...",
            f"pid={info['pid']} graph-org={info['graph_org']}",
        )
        # Tail that connector's own log for recent trouble, if we can find it.
        try:
            from tools.data_paths import DATA_ROOT

            net_dir = DATA_ROOT / "network"
            matches = sorted(net_dir.glob(f"serve-{org_uuid}-*.log"))
            if matches:
                log_path = matches[-1]
                tail = log_path.read_text(errors="replace").splitlines()[-5:]
                recent_4409 = sum(1 for l in tail if "close_code=4409" in l)
                if recent_4409:
                    _line(
                        f"  recent tunnel churn ({org_uuid[:8]}...)",
                        f"{recent_4409}/5 of last lines are 4409 (CLOSE_REPLACED) -- "
                        "another peer may be fighting for this org's serving slot",
                        warn=True,
                    )
        except Exception:
            pass


def check_org_resolution(report: dict) -> None:
    """The exact bug class that broke tonight's demo: does the org a fresh
    Fleet-serving publish would target actually have a connector running?"""
    _section("Org-resolution sanity (the bug that broke tonight)")
    try:
        from tools.graph.schemas.dashboard_shell import shell_default_org

        default_org_slug = shell_default_org()
        report["shell_default_org"] = default_org_slug
        _line("shell_default_org() resolves to", default_org_slug)

        running = report.get("running_connectors", {})
        # Map the default org's slug -> its org_uuid so we can compare against
        # the (org_uuid-keyed) running-connector set.
        from tools.graph.db import _org_db_path
        import sqlite3

        default_org_uuid = None
        try:
            path = _org_db_path(default_org_slug)
            if path.exists():
                conn = sqlite3.connect(str(path))
                row = conn.execute(
                    "SELECT payload FROM settings WHERE set_id='autonomy.network.binding' LIMIT 1"
                ).fetchone()
                if row:
                    default_org_uuid = json.loads(row[0]).get("org_uuid")
        except Exception:
            pass

        if default_org_uuid is None:
            _line(
                "default org's registry binding",
                "not found -- cannot confirm whether a connector exists for it",
                warn=True,
            )
        elif default_org_uuid in running:
            _line(
                "connector for shell_default_org",
                f"RUNNING ({default_org_uuid[:8]}...) -- fleet-serving publish would succeed",
            )
        else:
            _line(
                "connector for shell_default_org",
                f"NOT RUNNING ({default_org_uuid[:8]}...) -- "
                "publish_connector_runtime falls back to this org and WILL FAIL "
                "with TunnelUnavailable unless it's passed an explicit org=",
                fail=True,
            )
            if running:
                alt = ", ".join(o[:8] + "..." for o in running)
                _detail(f"        orgs that DO have a live connector: {alt}")
    except Exception as exc:
        _line("org-resolution check", f"FAILED to run: {exc!r}", fail=True)


# ── sync data ────────────────────────────────────────────────────────────

def check_sync_data(report: dict) -> None:
    _section("Personal-DB sync state")
    try:
        from tools.graph.db import _org_db_path
        import sqlite3

        path = _org_db_path("personal")
        report["personal_db_path"] = str(path)
        if not path.exists():
            _line("personal.db", "does not exist", fail=True)
            return
        size = path.stat().st_size
        _line("personal.db size", f"{size:,} bytes")
        report["personal_db_bytes"] = size
        conn = sqlite3.connect(str(path))
        conn.row_factory = None
        for table in ("thoughts", "sources", "fleet_sync_catalog"):
            try:
                n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                _line(f"  {table} rows", n)
                report[f"{table}_rows"] = n
            except sqlite3.OperationalError:
                _line(f"  {table} rows", "table does not exist", warn=True)
        try:
            triggers = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' "
                "AND name LIKE 'fleet_sync_%'"
            ).fetchone()[0]
            _line(
                "fleet_sync capture triggers active",
                bool(triggers),
                warn=not triggers,
            )
            report["fleet_sync_triggers_active"] = bool(triggers)
        except sqlite3.OperationalError:
            pass
    except Exception as exc:
        _line("sync-data check", f"FAILED to run: {exc!r}", fail=True)


def check_catalog_canary(report: dict) -> None:
    """Cheap, approximate signal for whether a checkpoint would even build.

    Found live 2026-08-23, hours into diagnosing a totally silent Fleet-sync
    failure: sync.py's real checkpoint builder refuses (AlphaError:
    "checkpoint contains untracked logical rows") whenever
    fleet_sync_catalog's live-row count doesn't match the actual row count
    across the replicated tables -- a real, versioned mismatch, not a
    phantom, caused here by rows bootstrapped before an earlier SQLite
    RETURNING-on-upsert fix landed. That failure is invisible anywhere
    except the SERVING CONNECTOR's own log file (never dashboard.log, never
    surfaced to the operator), and only fires mid-sync, so it cost hours to
    even locate.

    This does NOT call the real, authoritative check
    (MutationCatalog._verify_catalog_integrity) -- that does a full scan of
    the whole catalog AND every live row, the same expensive operation
    behind an earlier live freeze bug tonight (fixed by gating it out of
    the hot path). This is instead a handful of cheap COUNT(*) queries: a
    canary, not a guarantee. A mismatch here means "go run
    --verify-catalog to confirm and see the real numbers"; a match here is
    reassuring but not a full proof the way --verify-catalog is.
    """
    _section("Fleet-sync catalog canary (approximate -- see --verify-catalog for the real check)")
    try:
        import sqlite3
        from tools.graph.db import _org_db_path
        from tools.network.fleet_sync.policies import TABLE_POLICIES, PolicyKind

        path = _org_db_path("personal")
        if not path.exists():
            _line("personal.db", "does not exist", warn=True)
            return
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            tracked_live = conn.execute(
                "SELECT COUNT(*) FROM fleet_sync_catalog WHERE tombstone=0"
            ).fetchone()[0]
        except sqlite3.OperationalError:
            _line("fleet_sync_catalog", "table does not exist -- sync not yet activated", warn=True)
            return
        real_total = 0
        for table, policy in TABLE_POLICIES.items():
            if policy.kind in (PolicyKind.LOCAL, PolicyKind.DERIVED):
                continue
            try:
                real_total += conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.OperationalError:
                continue
        report["catalog_tracked_live"] = tracked_live
        report["catalog_real_total_approx"] = real_total
        if tracked_live == real_total:
            _line(
                "tracked_live vs real row count",
                f"{tracked_live:,} == {real_total:,} -- consistent (approximate check; "
                "not a full proof)",
            )
        else:
            _line(
                "tracked_live vs real row count",
                f"{tracked_live:,} != {real_total:,} -- MISMATCH. A real checkpoint attempt "
                "will likely fail with AlphaError('checkpoint contains untracked logical "
                "rows'), refusing silently from the operator's view (only the serving "
                "connector's own log shows it). Run --verify-catalog for the authoritative "
                "check and exact discrepancy before assuming this is real -- this canary "
                "isn't watermark-aware and can false-positive on rows mutated concurrently "
                "with this scan.",
                warn=True,
            )
    except Exception as exc:
        _line("catalog canary", f"FAILED to run: {exc!r}", fail=True)


def verify_catalog(*, org: str = "personal") -> dict:
    """The REAL, authoritative catalog-integrity check
    (MutationCatalog._verify_catalog_integrity) -- a full scan of the whole
    fleet_sync_catalog table plus every live row across every replicated
    table. Slow on a large personal.db (this is the same operation behind
    an earlier live freeze bug, deliberately never run automatically) --
    only invoke this explicitly, when the canary above raised a real
    question worth paying for a definitive answer.
    """
    import sqlite3
    from tools.graph.db import _org_db_path
    from tools.network.fleet_sync.catalog import MutationCatalog

    _section("Catalog integrity (authoritative, full scan -- this may take a while)")
    path = _org_db_path(org)
    if not path.exists():
        _line(f"{org}.db", "does not exist", fail=True)
        return {"ok": False}
    conn = sqlite3.connect(str(path))
    try:
        origin_incarnation = conn.execute(
            "SELECT origin_incarnation FROM fleet_sync_state"
        ).fetchone()
    except sqlite3.OperationalError:
        _line("fleet_sync_state", "table does not exist -- sync not yet activated", fail=True)
        return {"ok": False}
    if origin_incarnation is None:
        _line("fleet_sync_state", "no row -- sync not yet activated", fail=True)
        return {"ok": False}
    catalog = MutationCatalog(conn, origin_incarnation[0])
    try:
        live_rows, catalog_rows = catalog._verify_catalog_integrity()
    except Exception as exc:
        _line(
            "integrity check",
            f"FAILED: {exc!r} -- this is very likely the same class of problem blocking "
            "a real checkpoint; the exact failure text here is the authoritative answer",
            fail=True,
        )
        return {"ok": False, "error": repr(exc)}
    _line("live rows (real tables)", live_rows)
    _line("catalog rows (all, including tombstones)", catalog_rows)
    _line("integrity check", "passed -- every live row has winner metadata, every catalog row decodes and names a replicated table")
    return {"ok": True, "live_rows": live_rows, "catalog_rows": catalog_rows}


def repair_catalog(*, org: str = "personal", dry_run: bool = True) -> dict:
    """Run the two known Fleet-sync catalog reconciliation passes, real code
    (``MutationCatalog._repair_deprecated_settings_tombstones`` and
    ``._bootstrap_existing_rows``), not a re-derivation of their logic.

    Repairs the exact self-inconsistent case a retired base Setting can leave
    behind (a non-tombstone catalog winner whose journal frame carries
    ``deprecated=1`` -- see ``graph://da0ab997-b3e``), then bootstraps
    deterministic winner metadata for any live row the catalog has never
    recorded at all.

    Dry-run by default (the CLI's ``--yes`` flips it): both passes run for
    real inside one transaction -- their own progress bookkeeping needs real
    writes to iterate correctly, so a dry run cannot skip writing and still
    detect accurately -- and the transaction is rolled back instead of
    committed. Byte-identical detection either way; only whether it lands
    differs. A conflict either pass would raise on (a live row whose address
    the catalog already marks tombstoned) aborts the whole transaction --
    nothing partial is ever left in place.
    """
    import sqlite3
    from tools.graph.db import _org_db_path
    from tools.network.fleet_sync.catalog import MutationCatalog

    _section("Catalog reconciliation" + (" (dry run)" if dry_run else ""))
    path = _org_db_path(org)
    if not path.exists():
        _line(f"{org}.db", "does not exist", fail=True)
        return {"ok": False}
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        origin_incarnation = conn.execute(
            "SELECT origin_incarnation FROM fleet_sync_state"
        ).fetchone()
    except sqlite3.OperationalError:
        _line("fleet_sync_state", "table does not exist -- sync not yet activated", fail=True)
        return {"ok": False}
    if origin_incarnation is None:
        _line("fleet_sync_state", "no row -- sync not yet activated", fail=True)
        return {"ok": False}
    catalog = MutationCatalog(conn, origin_incarnation[0])

    conn.execute("BEGIN IMMEDIATE")
    try:
        repaired = catalog._repair_deprecated_settings_tombstones()
        bootstrapped = catalog._bootstrap_existing_rows()
    except Exception as exc:
        conn.rollback()
        conn.close()
        _line(
            "reconciliation",
            f"FAILED and rolled back: {exc!r} -- nothing written",
            fail=True,
        )
        return {"ok": False, "error": repr(exc)}

    verb = "would repair" if dry_run else "repaired"
    _line(f"{verb} (retired base -> proper tombstone)", repaired)
    verb = "would bootstrap" if dry_run else "bootstrapped"
    _line(f"{verb} (untracked live row -> winner metadata)", bootstrapped)

    if dry_run:
        conn.rollback()
        _line("dry run", "nothing written -- pass --yes to actually repair")
    else:
        conn.commit()
        _line("committed", f"{repaired + bootstrapped} catalog row(s) changed")
    conn.close()
    return {"ok": True, "repaired": repaired, "bootstrapped": bootstrapped, "dry_run": dry_run}


# ── recent errors ────────────────────────────────────────────────────────

def check_recent_errors(report: dict, *, tail_lines: int = 4000) -> None:
    _section(f"Recent Fleet-related errors (last {tail_lines} log lines per source)")
    try:
        from tools.data_paths import DATA_ROOT

        # NOTE some of these are one-sided: 'fleet server hello envelope is
        # malformed' is raised by fleet_relay_sync's PULLING side (the
        # machine dialing IN, e.g. a newly-enrolled member reaching for
        # home) -- it will never appear in the log of the machine being
        # dialed. Run this script on whichever side is actually failing,
        # not just on 'home', or this pattern silently never matches.
        patterns = [
            "TunnelUnavailable", "WatermarkError", "FleetRelaySyncError",
            "fleet server hello envelope is malformed",
            "serving machine is locked for Fleet sync",
            # Provisioning-ceremony failures during an unlock -- these are
            # request/response events, not persistent state, so this is the
            # only place fleet_doctor can see them at all. Caught live
            # 2026-08-23: a second unlock's registration retry 502'd because
            # post_register treats any non-201 registry response (including
            # 409 "already registered") as a hard failure -- state-at-rest
            # checks elsewhere in this script had no way to see that.
            "POST /api/network/register 5",
            "POST /api/network/serve-cert 5",
            "post_serve_cert:",
            # A checkpoint that can't actually be built -- see
            # check_catalog_canary. Only ever appears in a CONNECTOR's own
            # log (below), never dashboard.log -- this exact class of gap
            # (deep, connector-log-only errors invisible to every other
            # check) is why this function now reads connector logs at all.
            "AlphaError", "checkpoint contains untracked logical rows",
            "fleet relay sync stream failed", "fleet relay sync request refused",
        ]

        def _scan(label: str, log_path: Path) -> None:
            if not log_path.exists():
                _line(label, "not found at this path (may be elsewhere in a container)", warn=True)
                return
            lines = log_path.read_text(errors="replace").splitlines()[-tail_lines:]
            counts: dict[str, int] = {}
            last_seen: dict[str, str] = {}
            for line in lines:
                for pat in patterns:
                    if pat in line:
                        counts[pat] = counts.get(pat, 0) + 1
                        last_seen[pat] = line.strip()[:160]
            if not counts:
                _line(f"{label}: errors in recent log window", "none found")
            for pat, n in counts.items():
                _line(f"{label}: {pat!r}", f"{n}x -- last: {last_seen[pat]}", warn=True)
            report.setdefault("recent_error_counts", {})[label] = counts

        _scan("dashboard.log", DATA_ROOT / "dashboard.log")
        # A connector subprocess is a SEPARATE long-running process with its
        # own log file (data/network/serve-<org_uuid>-<pub>.log) -- every
        # deep exception tonight (the scheduler-config gap, the checkpoint
        # AlphaError) only ever showed up there, never in dashboard.log.
        # check_connectors already discovers these paths; do the same scan
        # here instead of leaving connector logs as a manual-grep-only spot.
        net_dir = DATA_ROOT / "network"
        for org_uuid, info in report.get("running_connectors", {}).items():
            matches = sorted(net_dir.glob(f"serve-{org_uuid}-*.log"))
            if matches:
                _scan(f"connector log ({org_uuid[:8]}...)", matches[-1])
    except Exception as exc:
        _line("recent-errors check", f"FAILED to run: {exc!r}", fail=True)


# ── reset: clear a stuck fleet:join invite chain ────────────────────────

def _find_stale_fleet_join_state() -> dict:
    """Locate every row tied to the current fleet:join invite target.

    Three independent stores each cache a piece of "publish a Fleet
    invite": the enrollment-invite record (machine.db,
    fleet_enrollment_invites), the link-grant that makes the connector
    eligible to serve it (settings, autonomy.network.link-grant, in
    whichever local org last published it), and the approval-request
    audit row the UI's "awaiting_signature" state resurfaces the newest
    of (approval_requests.db, kind=link_publish). None of these expire
    or get cleared on their own -- discovered live 2026-08-23 doing this
    exact cleanup by hand three times in one session, because a stuck
    target_uuid/token in any ONE of them silently reproduces the same
    stale link on every subsequent publish attempt.

    ``target_uuid`` for the generic fleet:join target is deterministic
    (the same value every time, by design -- it names "the" public
    join target, not a specific invite), so there is normally at most
    one enrollment-invite row; this reports whatever exists rather than
    assuming exactly one.
    """
    import sqlite3
    from tools.graph.db import _org_db_path
    from tools.graph import org_ops
    from tools.dashboard.dao import approval_requests as ar

    found = {"enrollment_invites": [], "link_grants": [], "approval_requests": []}

    machine_path = _org_db_path("machine")
    if machine_path.exists():
        conn = sqlite3.connect(machine_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT target_uuid, active, created_at FROM fleet_enrollment_invites"
            ).fetchall()
            found["enrollment_invites"] = [
                {"target_uuid": r["target_uuid"], "active": bool(r["active"]),
                 "created_at": r["created_at"], "db_path": str(machine_path)}
                for r in rows
            ]
        except sqlite3.OperationalError:
            pass

    org_slugs = ["autonomy"]  # scopeless local-store default publish target
    try:
        org_slugs.extend(ref.slug for ref in org_ops.list_orgs())
    except Exception:
        pass
    for slug in dict.fromkeys(org_slugs):  # de-dup, keep order
        path = _org_db_path(slug)
        if not path.exists():
            continue
        conn = sqlite3.connect(path)
        try:
            rows = conn.execute(
                "SELECT id, key, payload FROM settings "
                "WHERE set_id='autonomy.network.link-grant'"
            ).fetchall()
        except sqlite3.OperationalError:
            continue
        for row_id, key, payload in rows:
            try:
                p = json.loads(payload)
            except Exception:
                continue
            if p.get("target_type") == "fleet:join":
                found["link_grants"].append({
                    "id": row_id, "key": key, "org": slug,
                    "target_uuid": p.get("target_uuid"),
                })

    try:
        for row in ar.recent_for_kind("link_publish", limit=50):
            req = row.get("request") or {}
            if req.get("target_type") == "fleet:join":
                found["approval_requests"].append({
                    "id": row["id"], "created_at": row["created_at"],
                    "org": req.get("org"), "target_uuid": req.get("target_uuid"),
                })
    except Exception:
        pass

    return found


def clear_stale_fleet_join(*, dry_run: bool = True) -> dict:
    """Clear every stored piece of the current fleet:join invite chain.

    Dry-run by default: reports what it would remove without touching
    anything. Pass dry_run=False (the CLI's --yes) to actually delete.
    Removes across all three stores found by _find_stale_fleet_join_state
    -- clearing only one leaves the other two ready to resurface the
    same stale link on the next publish attempt, which is exactly what
    happened twice live before this existed.
    """
    import sqlite3
    from tools.graph.db import _org_db_path

    found = _find_stale_fleet_join_state()
    total = sum(len(v) for v in found.values())
    _section("Clear stale fleet:join invite state")
    if total == 0:
        _line("nothing found", "no enrollment-invite, link-grant, or approval-request "
              "rows are tied to a fleet:join target -- nothing to clear")
        return found

    for item in found["enrollment_invites"]:
        _line(
            "enrollment invite" + ("" if dry_run else " -- REMOVING"),
            f"target_uuid={item['target_uuid']} active={item['active']} "
            f"(machine.db)",
        )
    for item in found["link_grants"]:
        _line(
            "link-grant" + ("" if dry_run else " -- REMOVING"),
            f"org={item['org']} key={item['key']} target_uuid={item['target_uuid']}",
        )
    for item in found["approval_requests"]:
        _line(
            "approval_requests row" + ("" if dry_run else " -- REMOVING"),
            f"id={item['id']} org={item['org']} target_uuid={item['target_uuid']}",
        )

    if dry_run:
        _line("dry run", f"{total} row(s) found, none removed -- pass --yes to actually clear")
        return found

    machine_path = _org_db_path("machine")
    conn = sqlite3.connect(machine_path)
    conn.execute("DELETE FROM fleet_enrollment_invites")
    conn.commit()
    conn.close()

    for item in found["link_grants"]:
        subprocess.run(
            ["graph", "set", "remove", item["id"], "--org", item["org"]],
            check=False, capture_output=True, text=True,
        )

    if found["approval_requests"]:
        from tools.dashboard.dao import approval_requests as ar
        conn = sqlite3.connect(ar.DB_PATH)
        for item in found["approval_requests"]:
            conn.execute("DELETE FROM approval_requests WHERE id=?", (item["id"],))
        conn.commit()
        conn.close()

    _line("cleared", f"{total} row(s) removed -- next publish will mint a genuinely fresh invite")
    return found


def kick_roster_entry(setting_id: str, *, dry_run: bool = True) -> dict:
    """Remove ONE named roster entry, by the Settings id check_roster
    prints next to it (--kick-roster-entry <id>).

    Deliberately narrow and explicit -- a machine_id/entry the operator
    names, not an auto-detected "this looks stale" guess. Staleness
    isn't decidable from the data alone (a disconnected-but-real machine
    looks identical to a wiped-and-never-returning one), so this stays a
    targeted tool, not a "clean everything" button. This is a raw
    removal of the row (matching tonight's precedent), not a signed
    root-authored KICK tombstone -- fine for clearing dead debris, but
    not the cryptographically proper way to retire a still-real machine
    from a fleet with other live members watching the roster.
    """
    import sqlite3
    from tools.graph.db import _org_db_path

    conn = sqlite3.connect(_org_db_path("personal"))
    row = conn.execute(
        "SELECT payload FROM settings WHERE set_id='autonomy.fleet.roster' AND id=?",
        (setting_id,),
    ).fetchone()
    _section("Kick one roster entry")
    if row is None:
        _line("not found", f"no autonomy.fleet.roster row with id={setting_id}", fail=True)
        return {"found": False}
    _line(
        "entry" + ("" if dry_run else " -- REMOVING"),
        f"id={setting_id} payload={row[0][:200]}",
    )
    if dry_run:
        _line("dry run", "not removed -- pass --yes to actually remove")
        return {"found": True, "removed": False}
    result = subprocess.run(
        ["graph", "set", "remove", setting_id, "--org", "personal"],
        check=False, capture_output=True, text=True,
    )
    ok = result.returncode == 0
    _line("removed" if ok else "removal failed", result.stdout.strip() or result.stderr.strip(), fail=not ok)
    return {"found": True, "removed": ok}


def _run_remote(ssh_target: str, remote_cmd: str, forwarded_args: list[str]) -> int:
    """Re-invoke this same script on a remote node over SSH and relay its
    output, instead of duplicating any diagnostic logic for a second
    environment.

    ssh_target is a full destination string, whatever `ssh <that string>`
    would accept verbatim (host, user@host, or -p/-i flags folded in --
    e.g. "-i ~/.ssh/sjc -p 1226 jeremy@50.117.122.254"). remote_cmd is the
    command prefix that runs Python in the right place on that node --
    a bare venv host uses ".venv/bin/python3 -m tools.network.fleet_doctor";
    a containerized node (SJC's actual shape tonight) needs
    "docker exec autonomy-shipped-dashboard-1 python3 -m tools.network.fleet_doctor".
    fleet_doctor does not guess this -- it's supplied by the caller, who
    knows the target's actual deployment shape.
    """
    ssh_argv = ["ssh"] + ssh_target.split()
    remote_argv = remote_cmd.split() + forwarded_args
    full_cmd = ssh_argv + [subprocess.list2cmdline(remote_argv)]
    result = subprocess.run(full_cmd)
    return result.returncode


def main() -> int:
    global _QUIET
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit the collected report as JSON instead of text")
    parser.add_argument(
        "--clear-stale-invite", action="store_true",
        help="clear the stuck fleet:join invite/grant/publish-record chain "
             "(dry run unless --yes is also given)",
    )
    parser.add_argument(
        "--kick-roster-entry", metavar="SETTING_ID",
        help="remove one named roster entry by the Settings id check_roster "
             "prints next to it (dry run unless --yes is also given)",
    )
    parser.add_argument("--yes", action="store_true", help="actually act with --clear-stale-invite/--kick-roster-entry/--repair-catalog (default is dry run)")
    parser.add_argument(
        "--ssh", metavar="DESTINATION",
        help="run this same diagnostic on a remote node instead of locally -- "
             "everything else you pass still applies, just executed there. "
             "e.g. --ssh '-i ~/.ssh/sjc -p 1226 jeremy@50.117.122.254'",
    )
    parser.add_argument(
        "--remote-cmd", default=".venv/bin/python3 -m tools.network.fleet_doctor",
        help="command prefix to run on the remote node with --ssh (default assumes "
             "a bare venv host checkout; a containerized node needs something like "
             "'docker exec <container> python3 -m tools.network.fleet_doctor')",
    )
    parser.add_argument(
        "--verify-catalog", action="store_true",
        help="run the REAL, authoritative Fleet-sync catalog integrity check "
             "(full scan -- slow on a large personal.db, deliberately not part "
             "of the default report; run this when the canary in the default "
             "report raises a real question)",
    )
    parser.add_argument(
        "--repair-catalog", action="store_true",
        help="run the two known Fleet-sync catalog reconciliation passes "
             "(retired-base tombstone repair + untracked-live-row bootstrap) "
             "(dry run unless --yes is also given)",
    )
    args = parser.parse_args()
    _QUIET = args.json

    if args.ssh:
        forwarded = []
        if args.json:
            forwarded.append("--json")
        if args.clear_stale_invite:
            forwarded.append("--clear-stale-invite")
        if args.kick_roster_entry:
            forwarded += ["--kick-roster-entry", args.kick_roster_entry]
        if args.verify_catalog:
            forwarded.append("--verify-catalog")
        if args.repair_catalog:
            forwarded.append("--repair-catalog")
        if args.yes:
            forwarded.append("--yes")
        return _run_remote(args.ssh, args.remote_cmd, forwarded)

    if args.clear_stale_invite:
        clear_stale_fleet_join(dry_run=not args.yes)
        return 0
    if args.kick_roster_entry:
        kick_roster_entry(args.kick_roster_entry, dry_run=not args.yes)
        return 0
    if args.verify_catalog:
        verify_catalog()
        return 0
    if args.repair_catalog:
        repair_catalog(dry_run=not args.yes)
        return 0

    report: dict = {}
    if not args.json:
        print("Fleet diagnostic report")
        print("=" * 60)

    check_verdict(report)
    check_identity(report)
    check_local_store_migration(report)
    check_roster(report)
    check_connectors(report)
    check_serving_readiness(report)
    check_org_resolution(report)
    check_sync_data(report)
    check_catalog_canary(report)
    check_recent_errors(report)

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print("\n" + "=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
