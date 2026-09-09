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

Usage — DRY RUN IS THE DEFAULT, and prints exactly what a real run would do::

    python -m tools.dashboard.migrate_session_uploads_to_machine --org autonomy
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


def migrate(org: str, *, apply: bool) -> dict:
    from tools.dashboard.session_monitor import _agent_runs_root
    from tools.graph import settings_ops as ops

    root = _agent_runs_root()
    members = ops.read_set(SESSION_UPLOAD_SET_ID, org=org).members
    claimed: list[str] = []
    absent: list[str] = []
    for member in members:
        payload = member.payload or {}
        if _file_is_here(root, payload) is None:
            absent.append(member.key)
            continue
        claimed.append(member.key)
        if apply:
            # A LITERAL org=None, never CALLER_ORG: `org` is required here on
            # purpose, and the sentinel would consult the ambient cascade and
            # land on a slug. None means "scopeless explicit", which is what
            # lets the pinned home route the write (settings_ops `_open`: a
            # machine home IS the destination). The key is preserved, so
            # re-running claims nothing new.
            ops.add_setting(
                SESSION_UPLOAD_SET_ID, SCHEMA_REVISION, member.key, payload,
                org=None,
            )
    return {"root": str(root), "total": len(members),
            "claimed": claimed, "absent": absent, "applied": apply}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--org", required=True,
                        help="organization store holding the legacy rows")
    parser.add_argument("--apply", action="store_true",
                        help="actually write; omitted means dry run")
    args = parser.parse_args()
    result = migrate(args.org, apply=args.apply)
    verb = "claimed" if args.apply else "would claim"
    print(f"agent-runs: {result['root']}")
    print(f"{len(result['claimed'])} of {result['total']} rows {verb} by this "
          f"machine; {len(result['absent'])} left for another machine")
    for key in result["absent"]:
        print(f"  not here: {key}")


if __name__ == "__main__":
    main()
