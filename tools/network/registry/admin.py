"""Registry administrator CLI: domain show/reserve and existing deployment."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
import sys
import time

from .domains import reserve_domain

DEFAULT_DB = "/var/lib/autonomy-registry/registry.db"
APP_DIR = "/opt/autonomy-registry"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", help="administrator SSH target, e.g. root@registry.auto.network")
    parser.add_argument("--db", default=DEFAULT_DB, help="existing database on the registry host")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("deploy", help="run existing deploy.sh; inherits SMOKE_LINK")
    domain = sub.add_parser("domain").add_subparsers(dest="operation", required=True)
    domain.add_parser("show").add_argument("domain")
    reserve = domain.add_parser("reserve")
    reserve.add_argument("domain")
    reserve.add_argument("--org", required=True)
    args = parser.parse_args(argv)
    try:
        if args.target and not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.@:-]*", args.target):
            raise ValueError("invalid SSH target")
        if args.command == "deploy":
            if not args.target:
                raise ValueError("deploy requires --target")
            script = Path(__file__).parent / "deploy" / "deploy.sh"
            return subprocess.run(["bash", str(script), args.target], check=False).returncode
        if args.target:
            remote = [f"{APP_DIR}/venv/bin/python", "-m", "tools.network.registry.admin",
                      "--db", args.db, "domain", args.operation, args.domain]
            if args.operation == "reserve":
                remote.extend(["--org", args.org])
            command = f"cd {shlex.quote(APP_DIR)} && exec {shlex.join(remote)}"
            return subprocess.run(["ssh", "-o", "BatchMode=yes", "--", args.target, command],
                                  check=False).returncode
        path = Path(args.db)
        if not path.is_file():
            raise ValueError("registry database does not exist; refusing to create it")
        db_uid = path.stat().st_uid
        if os.geteuid() not in (0, db_uid):
            raise ValueError("on-box administration requires root or the database service account")
        if os.geteuid() == 0 and db_uid != 0:
            # Use the deployed service identity/StateDirectory, not root-owned
            # SQLite sidecars. Starting this transient command requires root.
            command = ["systemd-run", "--pipe", "--wait", "--collect",
                       "-p", "User=autonomy-registry", "-p", "DynamicUser=yes",
                       "-p", "StateDirectory=autonomy-registry",
                       f"--working-directory={APP_DIR}", sys.executable,
                       "-m", "tools.network.registry.admin", "--db", str(path),
                       "domain", args.operation, args.domain]
            if args.operation == "reserve":
                command.extend(["--org", args.org])
            return subprocess.run(command, check=False).returncode
        if args.operation == "show":
            # A read-only check must not initialize schemas or migrate a DB.
            with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
                conn.row_factory = sqlite3.Row
                from .domains import validate_managed_domain
                name = validate_managed_domain(args.domain)
                row = conn.execute("SELECT * FROM serve_zones WHERE zone = ?", (name,)).fetchone()
                member = conn.execute("SELECT persona_pub FROM serve_labels WHERE label = ?",
                                      (name.split(".")[0],)).fetchone()
                result = {"domain": name, "available": row is None and member is None,
                          "reservation": dict(row) if row is not None else None,
                          "member_owner": member["persona_pub"] if member is not None else None}
        else:
            from .store import RegistryStore
            store = RegistryStore(str(path))
            try:
                result = reserve_domain(store, args.domain, args.org, now=int(time.time()))
            finally:
                store.close()
        print(json.dumps(result, sort_keys=True))
        return 0
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error) as exc:
        print(f"registry operation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
