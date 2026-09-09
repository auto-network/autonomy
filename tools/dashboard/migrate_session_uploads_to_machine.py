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
copied; a row whose file is absent is left alone and reported AS ABSENT — never
as belonging to some other machine, which this tool cannot see. Run it on each
machine; each one claims its own rows and no row is claimed twice.

Deliberately NOT done here:

* **Nothing is deleted.** The organization-homed rows stay where they are.
  After the schema change nothing reads them (a machine home wins over the
  caller's org on the read path), so they are inert rather than harmful, and
  deleting replicated rows across a fleet is a separate decision with a
  separate blast radius. A later sweep can remove them once every machine has
  claimed what is its.
* **No guessing about destination.** An absent file might be on another machine
  or might be genuinely gone. The tool reports which of the two it can actually
  distinguish — whether the session's run directory still exists — and never
  claims a machine holds it. The first 13 unclaimed rows were reported as
  "left for another machine"; all 13 were deleted uploads from two old host
  sessions, and the machine implied had never run a session at all.

**Run it where the session run dirs are.** That is the dashboard container
(`/app/data/agent-runs`), NOT the host — on the host `_agent_runs_root()`
resolves to a directory that exists and is empty, which would report every row
as unclaimed. The tool refuses rather than reporting that.

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


def _host_uploads_dir() -> Path:
    """Where HOST-terminal sessions keep their uploads.

    A host session has no run dir at all, so its uploads live under
    `data/host-uploads/<tmux_name>/`. Mirrors `HOST_UPLOADS_DIR` in
    ``server.py``, which is the path the viewer actually serves from.
    """
    from tools.dashboard.server import HOST_UPLOADS_DIR

    return HOST_UPLOADS_DIR


def _run_dirs(root: Path, session: str) -> list[Path]:
    """Every directory that could hold *session*'s uploads.

    TWO KINDS OF SESSION, and searching only one of them is what made this
    tool wrong three times about the same 13 rows. A CONTAINER session stores
    uploads under `agent-runs/<tmux_name>` or `agent-runs/<tmux_name>-<stamp>`
    (several are possible). A HOST-terminal session has no run dir and stores
    them under `data/host-uploads/<tmux_name>/`.

    `server.py` has searched both since it served the first tile
    (`base_dirs` in the output route); this tool searched only the first, so
    every host-session upload looked absent. Both are candidates here for the
    same reason they are there: the row does not record which kind of session
    wrote it.
    """
    found = []
    if root.is_dir():
        found.extend(sorted(
            p for p in root.iterdir()
            if p.is_dir() and (p.name == session or p.name.startswith(session + "-"))
        ))
    host_dir = _host_uploads_dir() / session
    if host_dir.is_dir():
        found.append(host_dir)
    return found


def classify(root: Path, payload: dict) -> str:
    """Why this row was not claimed -- `here`, `orphaned`, or `file-missing`.

    ABSENCE IS NOT A DESTINATION, and this function exists because the same 13
    rows drew three wrong explanations in one afternoon:

    1. "left for another machine" -- an inference printed as a finding. The
       operator refuted it: no session had ever run on the machine implied.
    2. "orphaned, the files were deleted" -- also wrong, and worse for being
       written into this docstring as settled.
    3. The truth: 11 of the 13 were on this machine the whole time, under
       `data/host-uploads/<session>/`, because they came from HOST sessions
       and `_run_dirs` searched only `agent-runs`. The bug was never in the
       data.

    So this reports only what it can see, and the reader should note that even
    `orphaned` means "no directory of either kind is here", never "deleted":

    * ``orphaned`` -- no directory of EITHER kind exists here for that session.
      Nothing on this machine will ever claim the row. Whether the file was
      deleted or lives somewhere this tool cannot see is not decided here.
    * ``file-missing`` -- the run directory IS here but the file is not. That
      is the genuinely odd case and the only one worth chasing.

    Neither names a machine.
    """
    session = payload.get("target_session") or ""
    if _file_is_here(root, payload) is not None:
        return "here"
    return "orphaned" if not _run_dirs(root, session) else "file-missing"


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

    Base rows only, which is all an append-only log with uuid keys SHOULD
    have. This deliberately does NOT reimplement override/exclusion
    resolution -- but skipping quietly is its own defect, so anything that is
    not a live base row is reported by :func:`non_base_rows` rather than
    subtracted from a total nobody can see. The first real dry run enumerated
    106 where raw SQL counted 107, and the gap was only noticed because
    somebody cross-checked by hand.
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


def non_base_rows(org: str) -> list[dict]:
    """Rows this tool will NOT copy, each classified — the count gap, named.

    Three things land here and they do not mean the same thing:

    * ``deprecated`` — correctly skipped. The row is retired; copying it would
      un-retire it in the new store.
    * ``override`` (``supersedes``) — **copying the base would migrate a STALE
      PAYLOAD**, because the value in force is base + patch and this tool
      copies bases. Not a skip to shrug at.
    * ``exclusion`` (``excludes``) — the key is excluded from resolution;
      copying its base would resurrect a row somebody removed.

    Only the first is safe to ignore, which is why the classification is
    reported rather than the count.
    """
    from tools.graph import settings_ops as ops

    db = ops._open_read(org, None)
    try:
        rows = db.conn.execute(
            "SELECT key, id, deprecated, supersedes, excludes FROM settings"
            "  WHERE set_id = ?"
            "    AND NOT (deprecated = 0 AND supersedes IS NULL"
            "             AND excludes IS NULL)",
            (SESSION_UPLOAD_SET_ID,),
        ).fetchall()
    finally:
        db.close()
    out = []
    for row in rows:
        if row["excludes"] is not None:
            kind = "exclusion"
        elif row["supersedes"] is not None:
            kind = "override"
        else:
            kind = "deprecated"
        out.append({"key": row["key"], "id": row["id"], "kind": kind})
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
    skipped = non_base_rows(org)
    # What the machine store ALREADY holds, read once rather than per row.
    already = {key for key, _ in legacy_rows("machine")} if apply else set()
    skipped_present: list[str] = []
    claimed: list[str] = []
    unclaimed: list[dict] = []
    for key, payload in rows:
        kind = classify(root, payload)
        if kind != "here":
            unclaimed.append({"key": key, "kind": kind})
            continue
        claimed.append(key)
        if apply and key in already:
            # IDEMPOTENCE ACROSS OVERLAPPING STORES. The same key exists in
            # more than one legacy store (9 of personal's 10 were also in
            # autonomy), and `add_setting` inserts unconditionally -- so the
            # second pass hit a UNIQUE violation and died PART-WAY THROUGH,
            # having already written some rows. A migration that cannot be
            # re-run is a migration you cannot finish after any interruption.
            skipped_present.append(key)
            continue
        if apply and any(
            entry["key"] == key and entry["kind"] in ("override", "exclusion")
            for entry in skipped
        ):
            # This key's value in force is NOT its base row, and a base copy
            # would migrate a stale or resurrected value. Refuse the whole run
            # rather than write the wrong payload for one key inside an
            # otherwise correct migration, where it would be invisible.
            raise MigrationRefused(
                f"key {key} has an override or exclusion in the {org!r} store, "
                f"so its base row is not the value in force. Resolve that key "
                f"by hand before applying; this tool copies bases."
            )
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
    return {"root": str(root), "total": len(rows), "skipped": skipped,
            "claimed": claimed, "unclaimed": unclaimed, "applied": apply,
            "already_present": skipped_present}


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
    unclaimed = result["unclaimed"]
    orphaned = [e for e in unclaimed if e["kind"] == "orphaned"]
    missing = [e for e in unclaimed if e["kind"] == "file-missing"]
    present = result["already_present"]
    print(f"{len(result['claimed'])} of {result['total']} rows {verb} by this "
          f"machine; {len(unclaimed)} not claimed"
          + (f"; {len(present)} already in the machine store" if present else ""))
    # Say what is KNOWN (the file is not here) and never where it went. The
    # previous wording, "left for another machine", asserted a destination this
    # tool cannot see and got it wrong on every row it was applied to.
    if orphaned:
        print(f"  {len(orphaned)} orphaned — no run directory survives for the "
              f"session, so the upload is gone and the row outlived it:")
        for entry in orphaned:
            print(f"    {entry['key']}")
    if missing:
        print(f"  {len(missing)} file-missing — the run directory IS here but "
              f"the file is not; worth chasing:")
        for entry in missing:
            print(f"    {entry['key']}")
    # Printed ALWAYS, including when empty: the number a raw `count(*)` gives
    # differs from `total` by exactly these rows, and an unexplained gap is
    # how somebody ends up cross-checking by hand.
    skipped = result["skipped"]
    print(f"{len(skipped)} non-base row(s) not copied "
          f"(raw count = {result['total'] + len(skipped)})")
    for entry in skipped:
        note = "" if entry["kind"] == "deprecated" else \
            "  <-- value in force is NOT this base; resolve by hand"
        print(f"  {entry['kind']}: {entry['key']}{note}")


if __name__ == "__main__":
    main()
