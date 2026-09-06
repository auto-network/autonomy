"""Store enumeration for the backup scripts — the volume contract, applied.

The 2026-09-06 incident (auto-iwct5): backup-all.sh hand-listed databases
under its own checkout's ``data/`` and kept reporting "backup complete" for
5.5 days after the Compose cutover moved the real data to
``/opt/autonomy/data``. This module is the fix's spine: the shell scripts no
longer know any store by name — they ask this helper, which resolves every
row of :data:`tools.data_paths.STORE_MANIFEST` through :func:`resolve_store`
(the store's own env var → ``AUTONOMY_DATA_ROOT`` → the repository-local
legacy default), exactly like every other consumer of the contract.

Modes (one per argv[1], TAB-separated rows on stdout):

``stores``
    ``key  kind  action  required  relative  resolved_path`` for every
    manifest store. ``action`` is the backup policy:

    - ``sqlite``      — live-safe copy via sqlite3's ``.backup``
    - ``copy``        — plain copy preserving modes (key files / key dirs)
    - ``orgs-sqlite`` — a directory of SQLite DBs; ``.backup`` each ``*.db``
    - ``verify``      — existence-checked here; CONTENT is captured by the
      restic ``kind=data`` offsite snapshot (dedup makes the large artifact
      dirs affordable there; 17 full copies/day on the NAS would not be)

``offsite-data``
    Absolute paths for the restic ``kind=data`` snapshot: every ``verify``
    dir that exists, plus known legacy/off-manifest data dirs under the
    data root (attachments, uploads, experiments, voice-captures,
    voice-traces, chatgpt, claude). Off-manifest rows are best-effort:
    absent ones are simply omitted.

``extra-dbs``
    Absolute paths of ``*.db`` directly under the data root that no
    manifest row covers (e.g. ``experiments.db``, which is a live store
    but absent from the volume contract as of 2026-09-06). The backup
    captures these too — manifest drift must widen coverage, never
    silently narrow it.

``beads``
    ``database  host  port  user  password`` per Dolt database, mirroring
    the DAO contract (tools/dashboard/dao/beads.py:_conn_params):
    ``DOLT_SQL_*`` env → the beads dir's config.yaml / credentials.env /
    metadata.json → defaults (127.0.0.1:3306, root, empty password,
    database ``auto``). One row for the shared tracker plus one per
    provisioned org dir (``<beads root>/orgs/<slug>/metadata.json``).
    Prints nothing when the deployment has no beads root at all — the
    supported no-beads state.

Required-ness: every manifest store is required except the legacy /
proof-only VAPID keys; ``AUTONOMY_BACKUP_OPTIONAL_STORES`` (space-separated
store keys) extends the optional set without a code change.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.data_paths import (  # noqa: E402
    DATA_ROOT,
    STORE_MANIFEST,
    resolve_store,
)

# Backup policy per dir-kind store. DB and file kinds need no table
# (sqlite / copy respectively). An unlisted future dir store defaults to
# "verify" — offsite-only is the safe default for a directory of unknown
# size; the NAS tier must stay bounded.
_DIR_ACTIONS = {
    "orgs": "orgs-sqlite",       # the secret store: one .backup per org DB
    "serving_keys": "copy",      # small key material — belongs in every tier
    "web_push_keys": "copy",     # small key material — belongs in every tier
    "agent_runs": "verify",      # large; restic dedup handles it offsite
    "session_traces": "verify",  # large; restic dedup handles it offsite
    "dropbox": "verify",         # media; restic dedup handles it offsite
}

_OPTIONAL = {"web_push_vapid", "web_push_proof_vapid"} | set(
    os.environ.get("AUTONOMY_BACKUP_OPTIONAL_STORES", "").split()
)

# Live data dirs under the data root that predate (or sit outside) the
# volume contract. Best-effort offsite coverage; absence is not an error.
_OFF_MANIFEST_DATA_DIRS = (
    "attachments",
    "uploads",
    "experiments",
    "voice-captures",
    "voice-traces",
    "chatgpt",
    "claude",
)


def _action(store) -> str:
    if store.kind == "db":
        return "sqlite"
    if store.kind == "file":
        return "copy"
    return _DIR_ACTIONS.get(store.key, "verify")


def cmd_stores() -> None:
    for store in STORE_MANIFEST:
        required = "optional" if store.key in _OPTIONAL else "required"
        print(f"{store.key}\t{store.kind}\t{_action(store)}\t{required}"
              f"\t{store.relative}\t{resolve_store(store.key)}")


def cmd_offsite_data() -> None:
    for store in STORE_MANIFEST:
        if _action(store) == "verify":
            path = resolve_store(store.key)
            if path.exists():
                print(path)
    for name in _OFF_MANIFEST_DATA_DIRS:
        path = DATA_ROOT / name
        if path.exists():
            print(path)


def cmd_extra_dbs() -> None:
    covered = {resolve_store(s.key) for s in STORE_MANIFEST if s.kind == "db"}
    for path in sorted(DATA_ROOT.glob("*.db")):
        if path not in covered:
            print(path)


def _credential(beads_dir: Path, name: str) -> str | None:
    try:
        with open(beads_dir / "credentials.env", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if line.startswith("#"):
                    continue
                key, sep, val = line.partition("=")
                if sep and key == name:
                    return val
    except OSError:
        pass
    return None


def _config_host_port(beads_dir: Path) -> tuple[str | None, int | None]:
    host: str | None = None
    port: int | None = None
    try:
        with open(beads_dir / "config.yaml", encoding="utf-8") as fh:
            in_dolt = False
            for raw in fh:
                line = raw.rstrip("\n")
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                if not line[0].isspace():
                    in_dolt = line.strip().rstrip(":") == "dolt"
                    continue
                if in_dolt:
                    key, _, val = line.strip().partition(":")
                    val = val.strip()
                    if key == "host" and val:
                        host = val
                    elif key == "port" and val:
                        try:
                            port = int(val)
                        except ValueError:
                            pass
    except OSError:
        pass
    return host, port


def _dolt_database(beads_dir: Path) -> str | None:
    try:
        meta = json.loads((beads_dir / "metadata.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    db = meta.get("dolt_database")
    return db if isinstance(db, str) and db else None


def _pick(*values, default):
    for value in values:
        if value:
            return value
    return default


def _conn_row(beads_dir: Path) -> tuple[str, str, int, str, str]:
    cfg_host, cfg_port = _config_host_port(beads_dir)
    env = os.environ.get
    return (
        _pick(env("DOLT_SQL_DATABASE"), _dolt_database(beads_dir), default="auto"),
        _pick(env("DOLT_SQL_HOST"), cfg_host, default="127.0.0.1"),
        int(_pick(env("DOLT_SQL_PORT"), cfg_port, default=3306)),
        _pick(env("DOLT_SQL_USER"),
              _credential(beads_dir, "BEADS_DOLT_SERVER_USER"), default="root"),
        _pick(env("DOLT_SQL_PASSWORD"),
              _credential(beads_dir, "BEADS_DOLT_PASSWORD"), default=""),
    )


def cmd_beads() -> None:
    base = Path(os.environ.get("BEADS_DIR") or DATA_ROOT / ".beads")
    if not base.is_dir():
        return  # no beads in this deployment — the supported empty state
    rows = {}
    db, host, port, user, pw = _conn_row(base)
    rows[db] = (host, port, user, pw)
    for meta in sorted(base.glob("orgs/*/metadata.json")):
        db, host, port, user, pw = _conn_row(meta.parent)
        rows.setdefault(db, (host, port, user, pw))
    for db in sorted(rows):
        host, port, user, pw = rows[db]
        print(f"{db}\t{host}\t{port}\t{user}\t{pw}")


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    commands = {
        "stores": cmd_stores,
        "offsite-data": cmd_offsite_data,
        "extra-dbs": cmd_extra_dbs,
        "beads": cmd_beads,
    }
    if mode not in commands:
        print(f"usage: backup_stores.py {{{'|'.join(commands)}}}", file=sys.stderr)
        return 2
    commands[mode]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
