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


# ── roster ───────────────────────────────────────────────────────────────

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
        for machine_pub, entry in sorted(active.items()):
            marker = " <- this machine" if entry.machine_id == report.get("local_machine_id") else ""
            _detail(f"        machine_id={entry.machine_id[:16]}...  seq={entry.seq}{marker}")
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


# ── recent errors ────────────────────────────────────────────────────────

def check_recent_errors(report: dict, *, tail_lines: int = 4000) -> None:
    _section(f"Recent Fleet-related errors (last {tail_lines} log lines)")
    try:
        from tools.data_paths import DATA_ROOT

        log_path = DATA_ROOT / "dashboard.log"
        if not log_path.exists():
            _line("dashboard.log", "not found at this path (may be elsewhere in a container)", warn=True)
            return
        lines = log_path.read_text(errors="replace").splitlines()[-tail_lines:]
        patterns = [
            "TunnelUnavailable", "WatermarkError", "FleetRelaySyncError",
            "fleet server hello envelope is malformed",
            "serving machine is locked for Fleet sync",
        ]
        counts: dict[str, int] = {}
        last_seen: dict[str, str] = {}
        for line in lines:
            for pat in patterns:
                if pat in line:
                    counts[pat] = counts.get(pat, 0) + 1
                    last_seen[pat] = line.strip()[:160]
        if not counts:
            _line("errors in recent log window", "none found")
        for pat, n in counts.items():
            _line(f"{pat!r}", f"{n}x -- last: {last_seen[pat]}", warn=True)
        report["recent_error_counts"] = counts
    except Exception as exc:
        _line("recent-errors check", f"FAILED to run: {exc!r}", fail=True)


def main() -> int:
    global _QUIET
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit the collected report as JSON instead of text")
    args = parser.parse_args()
    _QUIET = args.json

    report: dict = {}
    if not args.json:
        print("Fleet diagnostic report")
        print("=" * 60)

    check_identity(report)
    check_roster(report)
    check_connectors(report)
    check_org_resolution(report)
    check_sync_data(report)
    check_recent_errors(report)

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print("\n" + "=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
