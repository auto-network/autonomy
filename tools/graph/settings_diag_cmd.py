"""``graph settings`` — what is declared, what is stored, and what is wrong.

One command for the question "is the settings substrate healthy". It answers
from three independent sources and says where each came from, because the
interesting failures are exactly where they disagree:

* the REGISTRY — what schemas the code declares, and what each says about
  itself: cardinality, key strategy, home, revisions.
* the DATABASES — what is actually stored, per organization: rows, distinct
  keys, payload bytes, override depth, deprecated rows.
* the LIVE PROCESS — throughput over the last ten and sixty seconds, read
  from the dashboard, which is the only process whose counters mean anything.

Validation runs over stored rows against their declared schema, so a row that
predates its contract, or a set with rows and no schema at all, is named
rather than discovered later by a failing read.

Reads only. Nothing here writes, promotes or deletes.
"""
from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any


# ── sources ──────────────────────────────────────────────────


def _api_base() -> str:
    return os.environ.get("GRAPH_API") or "https://localhost:8080"


def _get_json(path: str, *, org: str | None = None, timeout: int = 30) -> Any:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    headers = {"X-Graph-Org": org} if org else {}
    req = urllib.request.Request(f"{_api_base()}{path}", headers=headers)
    return json.load(urllib.request.urlopen(req, context=ctx, timeout=timeout))


def _registry_inventory() -> dict[str, dict]:
    """Every schema the code declares, with what it says about itself."""
    import importlib

    import tools.graph.schemas  # noqa: F401 — the auto-registered contracts
    from tools.graph.schemas import registry as R

    # Some schemas register only when a consumer imports their module, so the
    # package import alone under-counts. Sweep the tree the way the contract
    # tests do, otherwise "declared" silently means "declared and imported".
    repo_root = Path(__file__).resolve().parents[2]
    for path in list((repo_root / "tools").rglob("*.py")) + list(
            (repo_root / "agents").rglob("*.py")):
        text = str(path)
        if "/tests/" in text or path.name.startswith("test_"):
            continue
        try:
            if "SettingSchema)" not in path.read_text(errors="ignore"):
                continue
        except OSError:
            continue
        try:
            importlib.import_module(
                str(path.relative_to(repo_root))[:-3].replace("/", "."))
        except Exception:
            continue

    out: dict[str, dict] = {}
    for set_id in R.list_registered_set_ids():
        revisions = [r for r in range(1, 12) if R.get_schema(set_id, r)]
        if not revisions:
            continue
        newest = R.get_schema(set_id, revisions[-1])
        try:
            home = R.declared_home(set_id)
        except Exception:
            home = "conflict"
        out[set_id] = {
            "revisions": revisions,
            "fields": len(getattr(newest, "_field_metadata", None) or {}),
            "pattern": getattr(newest, "_access_pattern", None) or "—",
            "key_strategy": getattr(newest, "_key_strategy", None) or "—",
            "home": home or "—",
        }
    return out


def _org_databases(orgs_dir: Path) -> list[tuple[str, Path]]:
    """Every settings-bearing database: orgs from the glob, plus the local
    stores, which live BESIDE the directory since auto-35kmy — a footprint
    diagnostic that skipped the personal store would silently under-count.

    The local stores are resolved BY NAME through the routed helper and
    the glob never supplies them: during a both-locations conflict the
    glob's hit is the STALE file, and a diagnostic naming the file nothing
    reads is worse than none (peer review F4 — this is the tool an
    operator reaches for in exactly that state)."""
    from .db import LOCAL_STORE_SLUGS, _local_store_db_path

    out = (
        sorted(
            (p.stem, p) for p in orgs_dir.glob("*.db")
            if p.stem not in LOCAL_STORE_SLUGS
        )
        if orgs_dir.is_dir() else []
    )
    for name in LOCAL_STORE_SLUGS:
        try:
            local = _local_store_db_path(name, orgs_dir)
        except Exception:
            continue  # unreadable legacy file: already loud elsewhere
        if local.exists():
            out.append((name, local))
    return sorted(out)


def _scan_database(path: Path) -> dict[str, dict]:
    """Stored footprint per set_id in one database. Reads only."""
    import sqlite3

    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return {}
    try:
        rows = conn.execute(
            "SELECT set_id, schema_revision, key, publication_state, "
            "       supersedes, excludes, deprecated, LENGTH(payload) AS bytes, "
            "       payload "
            "FROM settings"
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()
    if not rows:
        return {}

    per_set: dict[str, dict] = {}
    for row in rows:
        entry = per_set.setdefault(row["set_id"], {
            "rows": 0, "keys": set(), "bytes": 0, "bases": 0,
            "overrides": 0, "excludes": 0, "deprecated": 0,
            "revisions": Counter(), "payloads": [],
        })
        entry["rows"] += 1
        entry["keys"].add(row["key"])
        entry["bytes"] += int(row["bytes"] or 0)
        entry["revisions"][int(row["schema_revision"])] += 1
        if row["deprecated"]:
            entry["deprecated"] += 1
        if row["excludes"] is not None:
            entry["excludes"] += 1
        elif row["supersedes"] is not None:
            entry["overrides"] += 1
        else:
            entry["bases"] += 1
            entry["payloads"].append(
                (row["key"], int(row["schema_revision"]), row["payload"]))
    return per_set


def _validate(set_id: str, payloads: list[tuple[str, int, str]]) -> list[str]:
    """Stored base rows that their own declared schema would reject."""
    from tools.graph.schemas import registry as R

    problems: list[str] = []
    for key, revision, raw in payloads:
        if R.get_schema(set_id, revision) is None:
            problems.append(f"{key} — stored at revision {revision}, which is "
                            f"not registered")
            continue
        try:
            R.validate_payload(set_id, revision, json.loads(raw))
        except json.JSONDecodeError:
            problems.append(f"{key} — payload is not JSON")
        except Exception as exc:
            problems.append(f"{key} — {str(exc)[:110]}")
    return problems


# ── rendering ────────────────────────────────────────────────


def _human_bytes(n: int) -> str:
    for unit in ("B", "K", "M", "G"):
        if n < 1024 or unit == "G":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n/1:.0f}{unit}"
        n /= 1024
    return f"{n:.0f}G"


def _size(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n/1024:.1f}K"
    return f"{n/1048576:.1f}M"


def cmd_settings_diag(args) -> None:
    orgs_dir = Path(
        os.environ.get("AUTONOMY_ORGS_DIR")
        or Path(__file__).resolve().parents[2] / "data" / "orgs"
    )
    registry = _registry_inventory()
    databases = _org_databases(orgs_dir)

    stored: dict[str, dict[str, dict]] = {}
    for slug, path in databases:
        scanned = _scan_database(path)
        if scanned:
            stored[slug] = scanned

    # ── overview ──
    total_rows = sum(e["rows"] for org in stored.values() for e in org.values())
    total_bytes = sum(e["bytes"] for org in stored.values() for e in org.values())
    total_keys = sum(len(e["keys"]) for org in stored.values() for e in org.values())
    print(f"\n  {len(registry)} schemas declared · {len(stored)} organization "
          f"database(s) · {total_rows} rows · {total_keys} keys · "
          f"{_size(total_bytes)}")
    if not stored:
        print(f"  (no readable database under {orgs_dir} — run this on the host "
              f"for stored-data figures)")

    # ── per organization ──
    if stored:
        print(f"\n  {'ORGANIZATION':16s} {'SETS':>5s} {'ROWS':>6s} {'KEYS':>6s} "
              f"{'OVERRIDES':>10s} {'DEPRECATED':>11s} {'SIZE':>8s}")
        print("  " + "─" * 70)
        for slug in sorted(stored):
            sets = stored[slug]
            print(f"  {slug:16s} {len(sets):5d} "
                  f"{sum(e['rows'] for e in sets.values()):6d} "
                  f"{sum(len(e['keys']) for e in sets.values()):6d} "
                  f"{sum(e['overrides'] for e in sets.values()):10d} "
                  f"{sum(e['deprecated'] for e in sets.values()):11d} "
                  f"{_size(sum(e['bytes'] for e in sets.values())):>8s}")

    # ── the largest sets ──
    combined: dict[str, dict] = {}
    for org_sets in stored.values():
        for set_id, entry in org_sets.items():
            agg = combined.setdefault(set_id, {
                "rows": 0, "keys": 0, "bytes": 0, "overrides": 0, "orgs": 0})
            agg["rows"] += entry["rows"]
            agg["keys"] += len(entry["keys"])
            agg["bytes"] += entry["bytes"]
            agg["overrides"] += entry["overrides"]
            agg["orgs"] += 1
    if combined:
        limit = getattr(args, "top", 12)
        print(f"\n  largest sets by stored bytes (top {limit})")
        print(f"  {'SET':44s} {'ORGS':>4s} {'ROWS':>5s} {'KEYS':>5s} "
              f"{'OVR':>4s} {'SIZE':>8s}  DECLARED")
        print("  " + "─" * 92)
        for set_id, agg in sorted(
                combined.items(), key=lambda kv: -kv[1]["bytes"])[:limit]:
            declared = registry.get(set_id)
            note = (f"{declared['pattern']}/{declared['home']}"
                    if declared else "NO SCHEMA")
            print(f"  {set_id[:44]:44s} {agg['orgs']:4d} {agg['rows']:5d} "
                  f"{agg['keys']:5d} {agg['overrides']:4d} "
                  f"{_size(agg['bytes']):>8s}  {note}")

    # ── disagreements ──
    stored_ids = set(combined)
    undeclared = sorted(stored_ids - set(registry))
    unused = sorted(set(registry) - stored_ids)
    if undeclared:
        print(f"\n  ! stored with no registered schema — nothing can validate "
              f"these ({len(undeclared)}):")
        for set_id in undeclared:
            print(f"      {set_id}")
    if unused and getattr(args, "verbose", False):
        print(f"\n  declared but unused ({len(unused)}):")
        for set_id in unused:
            print(f"      {set_id}")

    # ── rows the schema says cannot exist ──
    #
    # A set declaring singleton or keyed_per_entity is replaced, not amended.
    # The write path enforces that; resolution does not consult it, so an
    # override stored before the rule is still merged into every read. The
    # value served is one the schema declares impossible, and no reader can
    # tell -- which is exactly the state that cannot be found by reading.
    from tools.graph import settings_ops as _ops

    offenders: list[tuple[str, dict]] = []
    for slug, _path in databases:
        try:
            for row in _ops.illegal_amendments(org=slug):
                offenders.append((slug, row))
        except Exception:
            continue
    if offenders:
        print(f"\n  ! amendments on sets that declare replacement "
              f"({len(offenders)}) — merged into every read, and only "
              f"removable by id:")
        for slug, row in offenders:
            base = "" if row["base_present"] else "  [base already deleted]"
            print(f"      [{slug}] {row['id'][:12]}  {row['set_id']} "
                  f"key={row['key']!r}{base}")

    # ── sets that could not be read at all ──
    try:
        diag = _get_json("/api/diag/settings/sets", timeout=25)
        unreadable = [(r["set_id"], r["read_error"])
                      for r in (diag.get("sets") or [])
                      if r.get("read_error")]
    except Exception:
        unreadable = []
    if unreadable:
        print(f"\n  ! {len(unreadable)} set(s) could not be read — this is not "
              f"the same as being empty:")
        for set_id, error in unreadable:
            print(f"      {set_id}: {error}")

    # ── validation ──
    if not getattr(args, "no_validate", False):
        failures: list[tuple[str, str, str]] = []
        for slug, org_sets in stored.items():
            for set_id, entry in org_sets.items():
                for problem in _validate(set_id, entry["payloads"]):
                    failures.append((slug, set_id, problem))
        if failures:
            print(f"\n  ! {len(failures)} stored row(s) their own schema would "
                  f"reject:")
            for slug, set_id, problem in failures[:40]:
                print(f"      [{slug}] {set_id}: {problem}")
            if len(failures) > 40:
                print(f"      … and {len(failures) - 40} more")
        else:
            print("\n  every stored row validates against its declared schema")

    # ── live throughput ──
    try:
        stats = _get_json("/api/diag/settings", timeout=15)
    except Exception:
        print("\n  (the dashboard is not reachable, so live throughput is "
              "unavailable — its process holds the only counters that mean "
              "anything)")
        return
    print("\n  live throughput, from the dashboard process")
    print(f"  {'WINDOW':10s} {'CALLS':>7s} {'READS':>7s} {'WRITES':>7s} "
          f"{'UPSERTS':>8s} {'ERRORS':>7s}")
    print("  " + "─" * 52)
    for window in ("last_10s", "last_60s", "totals"):
        w = stats.get(window) or {}
        print(f"  {window:10s} {w.get('calls', 0):7d} {w.get('reads', 0):7d} "
              f"{w.get('writes', 0):7d} {w.get('upserts', 0):8d} "
              f"{w.get('errors', 0):7d}")
    last_error = stats.get("last_error")
    if last_error:
        print(f"  last error: {str(last_error)[:120]}")


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "settings",
        help="Inventory, validate and measure the settings substrate",
    )
    parser.add_argument("--top", type=int, default=12,
                        help="How many sets to list by size (default: 12)")
    parser.add_argument("--verbose", action="store_true",
                        help="Also list declared-but-unused schemas")
    parser.add_argument("--no-validate", action="store_true",
                        help="Skip validating stored rows against their schema")
    parser.set_defaults(func=cmd_settings_diag)
