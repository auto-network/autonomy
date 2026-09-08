"""One command that answers "is organization data actually moving between these
machines", instead of trading SQL between sessions.

Runs ``fleet_doctor --json`` on every target — locally, or over ssh into the
container where the live data is — and compares the collected evidence across
machines. The comparison is the point: no single machine can tell whether it
should have received something, so a per-machine report cannot answer the
question no matter how good it is.

What it decides, per organization scope:

* **captured** — rows the machine holds that the replication catalog covers.
  A row without catalog coverage can never cross, however many pulls run.
* **received** — rows carrying ANOTHER machine's row identifier. A settings
  row's frame carries every column including ``id``, and materialization
  matches on the logical address, so the identifier is whose write it was.
  Counting rows cannot distinguish delivery from a local migration: two
  machines that each converted the same history independently both reach the
  expected total while nothing has crossed. That false positive is exactly
  what happened on 2026-09-08 and is why this compares identifiers.
* **exclusive** — events one machine has and another does not. These are the
  honest delivery tests, because an address the receiver has no row for cannot
  be contested: an arriving mutation older than a local one at the same
  address is discarded, so a shared event can stay unmatched forever without
  anything being wrong.

Targets come from the Settings set ``autonomy.fleet.diagnostic-target``, so
the access map is durable and shared rather than pasted between sessions.
``--target`` overrides or adds one for a single run.

    python3 -m tools.network.fleet_sync_report
    python3 -m tools.network.fleet_sync_report --target 'sjc-2=-i ~/.ssh/sjc -p 11222 root@<tailnet-ip>'
    python3 -m tools.network.fleet_sync_report --json
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from dataclasses import dataclass, field

#: A containerized node: the host has no dependencies, and a host venv seat may
#: point at a pre-cutover data directory and report a live machine as
#: unenrolled, so the container is named explicitly.
DEFAULT_REMOTE_CMD = "docker exec -u autonomy autonomy-dashboard-1 python3"
COLLECT_TIMEOUT_S = 120


@dataclass
class Target:
    name: str
    ssh: str | None = None
    remote_cmd: str = DEFAULT_REMOTE_CMD
    role: str | None = None
    report: dict = field(default_factory=dict)
    error: str | None = None

    @property
    def reachable(self) -> bool:
        return self.error is None and bool(self.report)


def load_targets(explicit: list[str], include_local: bool) -> list[Target]:
    """Targets from Settings, plus any given on the command line."""
    targets: dict[str, Target] = {}
    try:
        from tools.graph import settings_ops
        from tools.graph.schemas.fleet_diagnostic_target import (
            FLEET_DIAGNOSTIC_TARGET_SET_ID as SET_ID,
        )

        for member in settings_ops.read_owned_set(SET_ID, org=None).members:
            payload = member.payload or {}
            targets[str(member.key)] = Target(
                name=str(member.key),
                ssh=payload.get("ssh") or None,
                remote_cmd=payload.get("remote_cmd") or DEFAULT_REMOTE_CMD,
                role=payload.get("role"),
            )
    except Exception:
        pass  # no set yet, or no personal store here: --target still works
    for raw in explicit:
        name, _, dest = raw.partition("=")
        name = name.strip()
        if not name:
            raise SystemExit(f"--target needs NAME=SSH_DESTINATION, got {raw!r}")
        targets[name] = Target(name=name, ssh=dest.strip() or None)
    if include_local and "local" not in targets:
        targets["local"] = Target(name="local", ssh=None)
    if not targets:
        raise SystemExit(
            "no targets. Add them to the Settings set "
            "autonomy.fleet.diagnostic-target, or pass "
            "--target NAME='-i ~/.ssh/key -p 22 root@host'."
        )
    return [targets[name] for name in sorted(targets)]


def collect(target: Target) -> None:
    """Run the doctor on one target and keep its JSON, or the reason not."""
    if target.ssh is None:
        argv = [sys.executable, "-m", "tools.network.fleet_doctor", "--json"]
    else:
        remote = shlex.split(target.remote_cmd) + [
            "-m", "tools.network.fleet_doctor", "--json",
        ]
        argv = ["ssh"] + shlex.split(target.ssh) + [subprocess.list2cmdline(remote)]
    try:
        done = subprocess.run(
            argv, capture_output=True, text=True, timeout=COLLECT_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        target.error = f"timed out after {COLLECT_TIMEOUT_S}s"
        return
    except Exception as exc:
        target.error = f"{type(exc).__name__}: {exc}"
        return
    if done.returncode != 0:
        tail = (done.stderr or done.stdout or "").strip().splitlines()
        target.error = f"exit {done.returncode}: {tail[-1] if tail else 'no output'}"
        return
    try:
        target.report = json.loads(done.stdout)
    except ValueError:
        head = (done.stdout or "").strip()[:160]
        target.error = f"output was not JSON: {head!r}"


def compare(targets: list[Target]) -> dict:
    """The cross-machine picture. Nothing here is a per-machine judgement."""
    live = [t for t in targets if t.reachable]
    scopes = sorted({
        scope
        for t in live
        for scope in (t.report.get("org_sync") or {})
        if scope != "personal"
    })
    result: dict = {"scopes": {}, "unreachable": {
        t.name: t.error for t in targets if not t.reachable
    }}
    for scope in scopes:
        per_machine: dict = {}
        ids: dict[str, dict[str, str]] = {}
        for t in live:
            entry = (t.report.get("org_sync") or {}).get(scope)
            if entry is None:
                continue
            ids[t.name] = entry.get("row_ids") or {}
            per_machine[t.name] = {
                "ledger_events": entry.get("ledger_events"),
                "published": entry.get("published"),
                "captured": entry.get("captured"),
                "uncaptured": (
                    None if entry.get("captured") is None
                    else max(0, (entry.get("published") or 0) - entry["captured"])
                ),
            }
        delivery: dict = {}
        exclusive: dict = {}
        for name, rows in ids.items():
            others = {o: r for o, r in ids.items() if o != name}
            received = {
                other: sum(
                    1 for key, rid in rows.items()
                    if key in other_rows and other_rows[key] == rid
                )
                for other, other_rows in others.items()
            }
            delivery[name] = received
            for other, other_rows in others.items():
                missing = sorted(set(other_rows) - set(rows))
                if missing:
                    exclusive.setdefault(name, {})[other] = missing[:8]
        result["scopes"][scope] = {
            "machines": per_machine,
            "received_from": delivery,
            "missing_from_peer": exclusive,
        }
    return result


def compare_frontiers(targets: list[Target]) -> dict:
    """Per scope, per origin: how far behind each machine is against the
    machine that is furthest ahead for that origin.

    This is the measurement that answers "is synchronization current", and it
    is only answerable by comparison. One machine's frontier age says nothing:
    an old frontier for an origin that has not written anything is correct,
    not stale. Only the DIFFERENCE between machines is a gap.

    Reported per origin rather than per scope because a scope can be current
    with one machine's writes and a day behind another's.
    """
    live = [t for t in targets if t.reachable]
    out: dict = {}
    for target in live:
        for scope, entry in (target.report.get("sync_frontiers") or {}).items():
            for origin in (entry.get("origins") or []):
                bucket = out.setdefault(scope, {}).setdefault(origin["origin"], {})
                bucket[target.name] = {
                    "newest_ns": origin["newest_ns"],
                    "age_s": origin["age_s"],
                    "transactions": origin["transactions"],
                }
    for scope, origins in out.items():
        for origin, machines in origins.items():
            ahead = max(machines.values(), key=lambda m: m["newest_ns"])
            for name, m in machines.items():
                m["behind_s"] = max(0, (ahead["newest_ns"] - m["newest_ns"]) // 1_000_000_000)
    return out


def _human(seconds: int) -> str:
    if seconds < 120:
        return f"{seconds}s"
    if seconds < 7200:
        return f"{seconds // 60}m"
    if seconds < 172800:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


#: A machine more than this far behind another for the same origin is lagging,
#: not merely idle. Deliberately generous; the live fault was 24 hours.
LAG_THRESHOLD_S = 900


#: Set by render() so the frontier verdict can name the empty channels that
#: explain a lag. Kept module-level rather than threaded through, because the
#: two reports are printed from one run and never composed elsewhere.
_EMPTY_BY_MACHINE: dict[str, list[str]] = {}


def _empty_channels_for(machine: str, scope: str) -> list[str]:
    return [
        channel for channel in _EMPTY_BY_MACHINE.get(machine, [])
        if channel.split("/")[0] == scope
    ]


def render_frontiers(frontiers: dict) -> int:
    """Print the lag table. Returns 1 if any machine is behind."""
    if not frontiers:
        return 0
    worst = 0
    print("\n== sync frontiers (behind = against the machine furthest ahead for that origin) ==")
    for scope, origins in sorted(frontiers.items()):
        for origin, machines in sorted(origins.items()):
            if len(machines) < 2:
                continue  # only one machine holds this origin: nothing to compare
            lagging = [
                (n, m) for n, m in machines.items() if m["behind_s"] >= LAG_THRESHOLD_S
            ]
            line = ", ".join(
                f"{n} {_human(m['behind_s'])} behind" if m["behind_s"] else f"{n} current"
                for n, m in sorted(machines.items())
            )
            mark = "FAIL" if lagging else "ok  "
            print(f"  [{mark}] {scope:<12} <- {origin[:12]}  {line}")
            if lagging:
                worst = 1
                for name, _m in lagging:
                    empty = _empty_channels_for(name, scope)
                    if empty:
                        print(
                            f"         {name} pulled {', '.join(empty)} and got "
                            "NOTHING while another machine is ahead: the server "
                            "agreed there was nothing past the position "
                            f"{name} claimed. Its claim is wrong, or the server "
                            "is not serving that scope."
                        )
    if worst:
        print(
            "  A machine behind here is NOT receiving writes another machine "
            "has. An idle scope is never reported here: this compares machines."
        )
    return worst


def _verdict(scope_data: dict) -> tuple[str, str]:
    """(state, sentence) for one scope. States: ok, stalled, uncaptured, alone."""
    machines = scope_data["machines"]
    if len(machines) < 2:
        return "alone", "only one machine reports this scope; nothing to compare"
    uncaptured = {n: m["uncaptured"] for n, m in machines.items() if m["uncaptured"]}
    if uncaptured:
        worst = ", ".join(f"{n} has {c} uncaptured" for n, c in sorted(uncaptured.items()))
        return "uncaptured", (
            f"{worst}. Rows the catalog does not cover can never cross, "
            "whatever the transport does."
        )
    crossed = any(
        count for received in scope_data["received_from"].values()
        for count in received.values()
    )
    if crossed:
        pairs = ", ".join(
            f"{name} holds {count} written by {other}"
            for name, received in sorted(scope_data["received_from"].items())
            for other, count in sorted(received.items()) if count
        )
        return "ok", f"data is crossing: {pairs}"
    missing = scope_data["missing_from_peer"]
    detail = ""
    if missing:
        name, per_other = sorted(missing.items())[0]
        other, keys = sorted(per_other.items())[0]
        detail = (
            f" {name} is missing {len(keys)}+ event(s) that {other} holds, "
            f"e.g. {keys[0][:16]}."
        )
    return "stalled", (
        "no machine holds a row another machine wrote. Every machine's rows "
        "are its own, so nothing has crossed." + detail
    )


def render(targets: list[Target], comparison: dict) -> int:
    _EMPTY_BY_MACHINE.clear()
    for target in targets:
        if target.reachable:
            _EMPTY_BY_MACHINE[target.name] = list(
                target.report.get("empty_pull_channels") or []
            )
    print("Fleet org-sync report")
    print("=" * 60)
    for target in targets:
        where = target.ssh or "local"
        if target.reachable:
            verdict = (target.report.get("verdict") or {}).get("top_line", "?")
            blind = target.report.get("process_scan_unavailable")
            extra = "  [process scan unavailable]" if blind else ""
            print(f"  [ok  ] {target.name:<12} {where}  verdict={verdict}{extra}")
        else:
            print(f"  [FAIL] {target.name:<12} {where}  {target.error}")
    worst = 0
    for scope, data in sorted(comparison["scopes"].items()):
        state, sentence = _verdict(data)
        mark = {"ok": "ok  ", "alone": "warn", "uncaptured": "FAIL", "stalled": "FAIL"}[state]
        print(f"\n== scope {scope} ==")
        print(f"  {'machine':<12} {'events':>7} {'rows':>7} {'captured':>9}  received-from")
        for name, m in sorted(data["machines"].items()):
            received = data["received_from"].get(name, {})
            got = ", ".join(f"{o}:{c}" for o, c in sorted(received.items()) if c) or "-"
            print(
                f"  {name:<12} {str(m['ledger_events'] or 0):>7} "
                f"{str(m['published'] or 0):>7} {str(m['captured']):>9}  {got}"
            )
        print(f"  [{mark}] {sentence}")
        if state in ("uncaptured", "stalled"):
            worst = 1
    if comparison["unreachable"]:
        worst = 1
    return worst


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--target", action="append", default=[], metavar="NAME=SSH",
        help="add or override a target for this run, e.g. "
             "--target 'sjc-2=-i ~/.ssh/sjc -p 11222 root@<tailnet-ip>'",
    )
    parser.add_argument(
        "--no-local", action="store_true",
        help="do not include the machine this command runs on",
    )
    parser.add_argument("--json", action="store_true", help="emit the comparison as JSON")
    args = parser.parse_args(argv)

    targets = load_targets(args.target, include_local=not args.no_local)
    for target in targets:
        collect(target)
    comparison = compare(targets)
    frontiers = compare_frontiers(targets)
    if args.json:
        print(json.dumps({
            "targets": {
                t.name: {"ssh": t.ssh, "role": t.role, "error": t.error}
                for t in targets
            },
            "comparison": comparison,
            "frontiers": frontiers,
        }, indent=2, default=str))
        return 0
    rc = render(targets, comparison)
    return max(rc, render_frontiers(frontiers))


if __name__ == "__main__":
    raise SystemExit(main())
