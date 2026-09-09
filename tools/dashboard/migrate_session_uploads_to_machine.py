"""One-time move of `dashboard.session.upload` rows into the machine store.

`SessionUploadV1` was `@home("organization")` and is now `@home("machine")`
(auto-wilkh), because the row names a file that exists on exactly one machine.
The schema change fixes every FUTURE upload; it does nothing for rows already
written, and reads now resolve to the machine store, so an existing row stops
rendering until it is moved.

**Attribution is by local file existence, and that is the honest test
available.** The row carries no machine identity — only `target_session` and a
`rel_path` under that session's run dir. So this tool asks the only question
that can be answered locally and correctly: *is the file here?* A row whose
file resolves under this machine's `agent-runs` belongs to this machine and is
copied; a row whose file is absent is left alone and reported. Run it on each
machine; each one claims its own rows and no row is claimed twice.

Deliberately NOT done here:

* **Nothing is deleted.** The organization-homed rows stay where they are.
  After the schema change nothing reads them (a machine home wins over the
  caller's org on the read path), so they are inert rather than harmful, and
  deleting replicated rows across a fleet is a separate decision with a
  separate blast radius. A later sweep can remove them once every machine has
  claimed what is its.
* **No guessing.** A file that is absent here might be on another machine or
  might be genuinely gone; this tool never decides which, it reports the row
  and moves on.

**Run it where the session run dirs are.** That is the dashboard container
(`/app/data/agent-runs`), NOT the host — on the host `_agent_runs_root()`
resolves to a directory that exists and is empty, which would report every row
as belonging to another machine. The tool refuses rather than reporting that.

**Legacy rows live in more than one store.** Measured 2026-09-09:
`autonomy.db` 107, `personal.db` 10, `machine.db` 0. Pass each source store in
turn; `--org` names where the OLD rows are read from, never where they land
(that is always the machine store, decided by the schema's home).

Usage — DRY RUN IS THE DEFAULT, and prints exactly what a real run would do::

    python -m tools.dashboard.migrate_session_uploads_to_machine --org autonomy
    python -m tools.dashboard.migrate_session_uploads_to_machine --org personal
    python -m tools.dashboard.migrate_session_uploads_to_machine --org autonomy --apply
"""

from __future__ import annotations

import argparse
from pathlib import Path

from tools.dashboard.session_upload_settings import (
    SCHEMA_REVISION,
    SESSION_UPLOAD_SET_ID,
)


def _run_dirs(root: Path, session: str) -> list[Path]:
    """Every run dir for *session*: the bare name and its timestamped forms.

    A run dir is `agent-runs/<tmux_name>` or `agent-runs/<tmux_name>-<stamp>`,
    and one session can have several. All are candidates; the file is looked
    for in each.
    """
    if not root.is_dir():
        return []
    return sorted(
        p for p in root.iterdir()
        if p.is_dir() and (p.name == session or p.name.startswith(session + "-"))
    )


def _file_is_here(root: Path, payload: dict) -> Path | None:
    """The local path of this row's upload, or None if it is not on this
    machine. `rel_path` is relative BY CONTRACT; anything absolute or escaping
    the run dir is refused rather than resolved."""
    session = payload.get("target_session") or ""
    rel = payload.get("rel_path") or ""
    if not session or not rel:
        return None
    relative = Path(rel)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    for run_dir in _run_dirs(root, session):
        candidate = run_dir / relative
        if candidate.is_file():
            return candidate
    return None


class MigrationRefused(RuntimeError):
    """The tool cannot see one of its inputs, so it refuses to report a result.

    Every failure this guards produces the SAME observable output as a
    completed migration -- "0 of 0 rows" -- which a reader would reasonably
    read as "already done". Both were hit for real on the first run
    (host-0906-222509, 2026-09-09): the host's agent-runs resolved to an empty
    directory while the real store was the container's, and the enumeration
    returned nothing while 117 rows existed.
    """


def legacy_rows(org: str) -> list[tuple[str, dict]]:
    """`(key, payload)` for the ORG-HOMED rows, read past the home redirect.

    `read_set` resolves the store through the schema's declared home, and this
    set's home is now `machine` -- so the ordinary read cannot see the very
    rows this tool exists to move, and returns zero without complaint. Passing
    `set_id=None` to the opener asks for the named org's database and nothing
    else; the redirect keys on the set_id it is not given.

    Base rows only, which is all an append-only log with uuid keys has. This
    deliberately does NOT reimplement override/exclusion resolution: a set that
    had those would need more than a payload copy, and silently copying half a
    resolution is worse than refusing.
    """
    import json

    from tools.graph import settings_ops as ops

    db = ops._open_read(org, None)
    try:
        rows = db.conn.execute(
            "SELECT key, payload FROM settings"
            "  WHERE set_id = ? AND deprecated = 0"
            "    AND supersedes IS NULL AND excludes IS NULL",
            (SESSION_UPLOAD_SET_ID,),
        ).fetchall()
    finally:
        db.close()
    out = []
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict):
            out.append((row["key"], payload))
    return out


def migrate(org: str, *, apply: bool) -> dict:
    from tools.dashboard.session_monitor import _agent_runs_root
    from tools.graph import settings_ops as ops

    root = _agent_runs_root()
    # REFUSE rather than report zero. An agent-runs directory that is missing
    # or empty means this process is not looking at the machine store it
    # thinks it is -- on the host it resolves to an empty
    # /opt/autonomy/code/data/agent-runs while the real one, with 2991
    # entries, is the dashboard container's /app/data/agent-runs. Reporting
    # "0 claimed" there is indistinguishable from an honest nothing-to-do.
    if not root.is_dir() or not any(root.iterdir()):
        raise MigrationRefused(
            f"agent-runs resolves to {root}, which is missing or empty -- this "
            f"is not the machine store that holds the uploads. Run this where "
            f"the session run dirs actually are (the dashboard container, not "
            f"the host)."
        )
    rows = legacy_rows(org)
    claimed: list[str] = []
    absent: list[str] = []
    for key, payload in rows:
        if _file_is_here(root, payload) is None:
            absent.append(key)
            continue
        claimed.append(key)
        if apply:
            # A LITERAL org=None, never CALLER_ORG: `org` is required here on
            # purpose, and the sentinel would consult the ambient cascade and
            # land on a slug. None means "scopeless explicit", which is what
            # lets the pinned home route the write (settings_ops `_open`: a
            # machine home IS the destination). The key is preserved, so
            # re-running claims nothing new.
            ops.add_setting(
                SESSION_UPLOAD_SET_ID, SCHEMA_REVISION, key, payload,
                org=None,
            )
    return {"root": str(root), "total": len(rows),
            "claimed": claimed, "absent": absent, "applied": apply}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--org", required=True,
                        help="store the LEGACY rows are read from (e.g. "
                             "autonomy, personal) — never where they land")
    parser.add_argument("--apply", action="store_true",
                        help="actually write; omitted means dry run")
    args = parser.parse_args()
    result = migrate(args.org, apply=args.apply)
    verb = "claimed" if args.apply else "would claim"
    print(f"agent-runs: {result['root']}")
    if result["total"] == 0:
        # Said plainly, because this is the sentence a stale tool would have
        # printed while 117 rows sat unread.
        print(f"no legacy rows in the {args.org!r} store — nothing to migrate "
              f"from here")
        return
    print(f"{len(result['claimed'])} of {result['total']} rows {verb} by this "
          f"machine; {len(result['absent'])} left for another machine")
    for key in result["absent"]:
        print(f"  not here: {key}")


if __name__ == "__main__":
    main()
