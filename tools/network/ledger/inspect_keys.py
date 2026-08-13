#!/usr/bin/env python3
"""Count the public keys in one org's authority ledger, without touching it.

    python3 -m tools.network.ledger.inspect_keys anchore
    python3 -m tools.network.ledger.inspect_keys --all

Must run on the HOST, where ``data/orgs/<slug>.db`` actually lives. Container
sessions cannot read another org's DB and should not try — ask the host.

WHY THIS EXISTS AS A TOOL RATHER THAN A ONE-LINER
=================================================

Reading an org ledger has three separate traps. Each one produces a wrong
answer that LOOKS right, which is why this is a file and not an idiom people
retype.

1. LedgerStore(path) IS A WRITE.
   Its __init__ runs ``executescript(_SCHEMA)``. Point it at an org that was
   never founded and it CREATES empty ledger_* tables — converting "this org
   has no ledger" into "this org has an empty ledger" and destroying the exact
   distinction you were measuring. A destructive read. This module never opens
   the live file with LedgerStore.

2. immutable=1 IS NOT THE STRONGEST READ-ONLY FLAG. IT IS THE WRONG ONE.
   Tempting, because SQLite's docs describe it as disabling all locking and
   change detection. But it also makes SQLite ignore the -wal file entirely,
   and org DBs run in WAL mode. On a live database it silently returns a stale
   view. Measured on a WAL db with 2 committed rows sitting in the WAL:

       truth (writer sees):        3
       mode=ro (replays WAL):      3   correct
       immutable=1 (ignores WAL):  1   WRONG, and silent

   Use ``mode=ro``. It respects locking and replays the WAL.

3. shutil.copy2 OF A LIVE SQLITE DB IS NOT A SNAPSHOT.
   It copies the main file without -wal, so it loses every committed page
   still in the WAL — the same silent under-count as above (1 instead of 3),
   and it can also tear if a write lands mid-copy. Use ``VACUUM INTO``, which
   is read-only against the source and writes a consistent snapshot that
   includes the WAL.

Provenance: written after auto-0807-165113 asked the host to count anchore's
ledger keys and correctly refused to open the live file with LedgerStore. Traps
2 and 3 were found while hardening their script and are demonstrated above with
real numbers, not asserted.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import tempfile
from pathlib import Path

for _parent in Path(__file__).resolve().parents:
    if (_parent / "tools" / "network" / "ledger").is_dir():
        sys.path.insert(0, str(_parent))
        break

from tools.network.ledger.store import LedgerStore, org_ledger_db_path  # noqa: E402


def _ro(path: Path) -> sqlite3.Connection:
    """Read-only connection that still replays the WAL. See trap 2."""
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def snapshot(path: Path, dest: Path) -> None:
    """Consistent, WAL-inclusive copy. Read-only against *path*. See trap 3."""
    conn = _ro(path)
    try:
        conn.execute("vacuum into ?", (str(dest),))
    finally:
        conn.close()


def inspect(slug: str) -> dict:
    """Return the ledger's key populations, or a reason there are none.

    Never writes to the org DB. Never instantiates LedgerStore on it.
    """
    path = org_ledger_db_path(slug)
    out: dict = {"org": slug, "db": str(path)}

    if not path.exists():
        out["result"] = "no org DB at that path"
        return out

    # Existence probe first. An org row in the registry does NOT imply a
    # founded ledger — `graph org retrofit-ledgers` exists precisely because
    # orgs predate ledgers. "Never founded" is a real answer, not a failure.
    conn = _ro(path)
    try:
        tables = {
            r[0] for r in conn.execute(
                "select name from sqlite_master "
                "where type='table' and name like 'ledger_%'"
            )
        }
        if "ledger_events" not in tables:
            out["result"] = "never founded — no ledger_* tables"
            out["events"] = 0
            return out
        out["events"] = conn.execute("select count(*) from ledger_events").fetchone()[0]
    finally:
        conn.close()

    if not out["events"]:
        out["result"] = "ledger tables exist but hold no events"
        return out

    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / f"{slug}.db"
        snapshot(path, copy)
        store = LedgerStore(copy)
        try:
            state = store.fold()
        finally:
            store.close()

    personas = set(state.members)
    current = {m.current_key for m in state.members.values()}
    roots = {state.root} | set(state.lineage)
    kem = {
        (m.kem_credential or {}).get("kem_pub")
        for m in state.members.values()
        if m.kem_credential
    } - {None}

    out.update({
        "result": "founded",
        "genesis": state.genesis_id,
        "root": state.root,
        "roots_incl_rotations": len(roots),
        "personas": len(personas),
        "rekeyed": len({p for p in personas if state.members[p].current_key != p}),
        "current_signing_keys": len(current),
        "kem_pubs": len(kem),
        "distinct_ed25519": len(roots | personas | current),
        "members": {p: sorted(m.roles) for p, m in state.members.items()},
    })
    return out


def _print(d: dict) -> None:
    print(f"org:    {d['org']}")
    print(f"db:     {d['db']}")
    if d.get("result") != "founded":
        print(f"RESULT: {d['result']}")
        print("        Public keys in its ledger: 0")
        return
    print(f"events: {d['events']}")
    print()
    print(f"genesis:            {d['genesis']}")
    print(f"org root (current): {d['root']}")
    print(f"  root lineage incl. rotations: {d['roots_incl_rotations']}")
    print(f"member personas (stable ids):   {d['personas']}")
    print(f"  of those, rekeyed away:       {d['rekeyed']}")
    print(f"distinct current signing keys:  {d['current_signing_keys']}")
    print(f"KEM credential pubs:            {d['kem_pubs']}")
    print()
    print(f"DISTINCT Ed25519 pubs total:    {d['distinct_ed25519']}")
    print("  (roots ∪ personas ∪ current signing keys; KEM pubs are X25519")
    print("   and counted separately — they are not signing identities.)")
    print()
    for pid, roles in sorted(d["members"].items()):
        print(f"  {pid[:16]}…  roles={','.join(roles) or '-'}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("org", nargs="?", help="org slug, e.g. anchore")
    ap.add_argument("--all", action="store_true",
                    help="every org DB under data/orgs/")
    ap.add_argument("--json", action="store_true", help="machine-readable")
    args = ap.parse_args(argv)

    if args.all:
        orgs = sorted(p.stem for p in org_ledger_db_path("x").parent.glob("*.db"))
    elif args.org:
        orgs = [args.org]
    else:
        ap.error("give an org slug or --all")

    results = [inspect(o) for o in orgs]
    if args.json:
        import json
        print(json.dumps(results, indent=2))
    else:
        for i, d in enumerate(results):
            if i:
                print("\n" + "─" * 60 + "\n")
            _print(d)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
