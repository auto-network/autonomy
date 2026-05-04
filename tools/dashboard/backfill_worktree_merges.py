"""One-shot CLI: backfill historical worktree merges into ``dispatch_runs``.

Walks ``master --first-parent`` since a date, classifies each commit, and
writes ``kind='worktree-merge'`` rows for the merge events that escaped
the live writer (``agents.dispatch_db.record_worktree_merge_run``). Reuses
the writer; idempotency comes for free via ``INSERT OR IGNORE`` on
``id = 'wt-{commit_hash[:12]}'``.

Usage:
    python -m tools.dashboard.backfill_worktree_merges \\
        [--since 2026-04-01] [--limit N] [--commit] [--repo PATH]

Defaults to a dry-run. The operator-review gate documented in bead
``auto-imr2q`` REQUIRES eyeballing dry-run output before passing
``--commit`` — see the bead's "Required manual verification gate".

Classification table:
    ``^merge: auto-``      → skip:dispatcher-merge   (already a bead row)
    ``^Merge branch ``     → skip:rebase-merge       (plumbing, not a merge event)
    commit_hash in DB      → skip:already-recorded   (live row already covers it)
    anything else          → would-write             (backfill candidate)

Backfill rows persist:
    commit_hash, commit_message  — from ``git log -1``
    completed_at = started_at    — committer time (writer extension)
    container_name               — extracted from ``(auto-XXXXX)`` token if present
    branch                       — NULL (source branch is gone after worktree cleanup)
    branch_base                  — 'master'
    reason                       — 'backfill' (sentinel distinguishing from live ff/cherry-pick/commit-merge)
    lines_added, lines_removed, files_changed — via the writer's ``--numstat`` lookup
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# A commit subject containing exactly one ``(auto-XXXXX)`` token marks
# the FF-merged session commit's session id. The token is the dispatcher
# bead id minted by ``agents.dispatcher`` and survives in the subject by
# the codex commit-message convention. Non-greedy alphanumeric so we
# don't swallow surrounding text.
SESSION_TOKEN_RE = re.compile(r"\(auto-([a-z0-9]+)\)")


@dataclass
class Classification:
    sha: str
    short_sha: str
    subject: str
    committer_iso: str
    kind: str
    detail: str
    session_token: str | None = None
    lines_added: int = 0
    lines_removed: int = 0
    files_changed: int = 0
    existing_run_id: str | None = None


def extract_session_token(subject: str) -> str | None:
    """Return ``auto-XXXXX`` from a commit subject, or ``None``."""
    m = SESSION_TOKEN_RE.search(subject)
    return f"auto-{m.group(1)}" if m else None


def parse_shortstat(text: str) -> tuple[int, int, int]:
    """Parse ``git diff --shortstat`` output: ``(added, removed, files)``.

    Each field is independently optional — pure-rename commits have no
    insertions/deletions but still report files changed; pure-deletion
    commits have no insertions; etc. Missing fields parse as 0.
    """
    files = 0
    added = 0
    removed = 0
    m = re.search(r"(\d+)\s+files?\s+changed", text)
    if m:
        files = int(m.group(1))
    m = re.search(r"(\d+)\s+insertions?\(\+\)", text)
    if m:
        added = int(m.group(1))
    m = re.search(r"(\d+)\s+deletions?\(-\)", text)
    if m:
        removed = int(m.group(1))
    return added, removed, files


def get_shortstat(repo: Path, sha: str) -> tuple[int, int, int]:
    """``(added, removed, files)`` for ``<sha>^..<sha>`` via ``--shortstat``."""
    try:
        result = subprocess.run(
            ["git", "diff", "--shortstat", f"{sha}^..{sha}"],
            capture_output=True, text=True, timeout=10,
            cwd=str(repo),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return 0, 0, 0
    if result.returncode != 0:
        return 0, 0, 0
    return parse_shortstat(result.stdout.strip())


def get_full_message(repo: Path, sha: str) -> str:
    result = subprocess.run(
        ["git", "log", "-1", "--pretty=format:%B", sha],
        capture_output=True, text=True, cwd=str(repo),
    )
    return result.stdout.rstrip("\n")


def walk_master(
    repo: Path, since: str, limit: int | None,
) -> list[tuple[str, str, str]]:
    """Yield ``(sha, committer_iso, subject)`` for first-parent master commits."""
    cmd = ["git", "log", "master", "--first-parent",
           f"--since={since}",
           "--pretty=format:%H%x09%cI%x09%s"]
    if limit:
        cmd.append(f"-{limit}")
    result = subprocess.run(
        cmd, capture_output=True, text=True, cwd=str(repo),
    )
    rows: list[tuple[str, str, str]] = []
    for line in result.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3:
            rows.append((parts[0], parts[1], parts[2]))
    return rows


def lookup_existing_run(conn: sqlite3.Connection, sha: str) -> str | None:
    row = conn.execute(
        "SELECT id FROM dispatch_runs WHERE commit_hash = ? LIMIT 1",
        (sha,),
    ).fetchone()
    return row[0] if row else None


def classify(
    sha: str, subject: str, committer_iso: str,
    conn: sqlite3.Connection, repo: Path,
) -> Classification:
    short_sha = sha[:7]
    if subject.startswith("merge: auto-"):
        return Classification(
            sha=sha, short_sha=short_sha, subject=subject,
            committer_iso=committer_iso,
            kind="skip:dispatcher-merge",
            detail=f"({subject!r})",
        )
    if subject.startswith("Merge branch "):
        return Classification(
            sha=sha, short_sha=short_sha, subject=subject,
            committer_iso=committer_iso,
            kind="skip:rebase-merge",
            detail=f"({subject!r})",
        )
    existing = lookup_existing_run(conn, sha)
    if existing:
        return Classification(
            sha=sha, short_sha=short_sha, subject=subject,
            committer_iso=committer_iso,
            kind="skip:already-recorded",
            detail=f"(in dispatch_runs as run_id={existing})",
            existing_run_id=existing,
        )
    token = extract_session_token(subject)
    added, removed, files = get_shortstat(repo, sha)
    detail_session = f"session={token}" if token else "session=NULL"
    detail = f"{detail_session}  +{added} -{removed}  {files} files"
    return Classification(
        sha=sha, short_sha=short_sha, subject=subject,
        committer_iso=committer_iso,
        kind="would-write", detail=detail,
        session_token=token, lines_added=added,
        lines_removed=removed, files_changed=files,
    )


def parse_committer_iso(committer_iso: str) -> datetime:
    """git ``%cI`` is strict ISO-8601 (with TZ). Returns a tz-aware datetime."""
    return datetime.fromisoformat(committer_iso)


def _default_since() -> str:
    return (
        datetime.now(timezone.utc) - timedelta(days=30)
    ).strftime("%Y-%m-%d")


def run(
    *,
    since: str | None = None,
    limit: int | None = None,
    repo: str | Path | None = None,
    do_commit: bool = False,
    out=sys.stdout,
) -> dict:
    """Importable entry point. Returns a summary dict for tests.

    Side effects: prints a per-commit line + a multi-line summary to ``out``.
    With ``do_commit=True``, calls ``record_worktree_merge_run`` for every
    ``would-write`` classification.
    """
    if since is None:
        since = _default_since()
    repo_path = Path(repo or REPO_ROOT).resolve()

    from agents import dispatch_db
    dispatch_db.init_db()

    conn = sqlite3.connect(str(dispatch_db.DB_PATH))
    try:
        commits = walk_master(repo_path, since, limit)
        classifications = [
            classify(sha, subject, committer_iso, conn, repo_path)
            for sha, committer_iso, subject in commits
        ]
    finally:
        conn.close()

    counts = {
        "skip:dispatcher-merge": 0,
        "skip:rebase-merge": 0,
        "skip:already-recorded": 0,
        "would-write": 0,
    }
    for c in classifications:
        out.write(f"{c.short_sha}  {c.kind:<24}{c.detail}\n")
        counts[c.kind] += 1

    out.write("\n")
    out.write(f"walked {len(classifications)} commits since {since}\n")
    out.write(
        f"skipped {counts['skip:dispatcher-merge']} dispatcher-merge / "
        f"{counts['skip:rebase-merge']} rebase / "
        f"{counts['skip:already-recorded']} already-recorded\n"
    )

    wrote = 0
    if do_commit:
        for c in classifications:
            if c.kind != "would-write":
                continue
            full_message = get_full_message(repo_path, c.sha)
            run_id = dispatch_db.record_worktree_merge_run(
                commit_hash=c.sha,
                commit_message=full_message,
                branch=None,
                branch_base="master",
                container_name=c.session_token,
                reason="backfill",
                target_repo=str(repo_path),
                completed_at=parse_committer_iso(c.committer_iso),
            )
            if run_id:
                wrote += 1
        out.write(f"wrote {wrote} backfill rows\n")
    else:
        out.write(
            f"would write {counts['would-write']} backfill rows  (dry-run)\n"
        )

    return {
        "classifications": classifications,
        "counts": counts,
        "wrote": wrote if do_commit else None,
        "since": since,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backfill historical worktree merges into dispatch_runs.",
    )
    parser.add_argument(
        "--since", default=None,
        help="git log --since= argument (default: 30 days ago, e.g. 2026-04-04).",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Max commits to walk; useful for the operator-review gate.",
    )
    parser.add_argument(
        "--repo", default=str(REPO_ROOT),
        help="Path to the autonomy repo (default: this checkout).",
    )
    parser.add_argument(
        "--commit", action="store_true", default=False,
        help="Actually write rows. Default is dry-run (no DB writes).",
    )
    parser.add_argument(
        "--dry-run", action="store_false", dest="commit",
        help="Print classifications but make no changes (default).",
    )
    args = parser.parse_args(argv)

    run(
        since=args.since,
        limit=args.limit,
        repo=args.repo,
        do_commit=args.commit,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
