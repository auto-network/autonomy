"""Tests for the worktree-merge backfill CLI (bead auto-imr2q).

Builds a small canned commit graph in a tmp git repo + a tmp dispatch.db
and exercises the four classification paths plus the writer integration.

The writer extension that lets ``completed_at`` come from the commit's
committer time is verified end-to-end in
``test_backfill_persists_committer_time_as_completed_at`` so the timeline
row lands at the original merge moment instead of "now".
"""

from __future__ import annotations

import importlib
import io
import os
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path

import pytest


# ── Fixtures ──────────────────────────────────────────────────────────


def _git(repo: Path, *args: str, env_extra: dict | None = None) -> str:
    env = os.environ.copy()
    env.update(env_extra or {})
    result = subprocess.run(
        ["git", *args],
        capture_output=True, text=True, cwd=str(repo), env=env, check=True,
    )
    return result.stdout


def _commit(
    repo: Path, *, name: str, content: str, subject: str,
    when: str | None = None,
) -> str:
    """Write ``name`` with ``content`` and commit with ``subject``.

    ``when`` is an ISO date string committer + author dates land at —
    needed so the ``--since`` filter test has predictable ages.
    """
    (repo / name).write_text(content)
    _git(repo, "add", name)
    env_extra = {}
    if when is not None:
        env_extra["GIT_AUTHOR_DATE"] = when
        env_extra["GIT_COMMITTER_DATE"] = when
    _git(repo, "commit", "-m", subject, env_extra=env_extra)
    return _git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Initialize a fresh git repo on branch ``master``."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "master")
    _git(r, "config", "user.email", "test@example.com")
    _git(r, "config", "user.name", "Test")
    _git(r, "config", "commit.gpgsign", "false")
    # Seed commit so HEAD^ exists for the first measured commit. The
    # subject deliberately matches the dispatcher-merge skip pattern so
    # the seed never lands as a "would-write" candidate when tests walk
    # ``--since=1970-01-01`` (the oldest reachable commit on master in
    # production is older than any ``--since`` an operator would pass).
    _commit(r, name="seed.txt", content="seed\n",
            subject="merge: auto-seed — fixture seed")
    return r


@pytest.fixture
def isolated_dispatch_env(tmp_path, monkeypatch):
    """Pin DISPATCH_DB to a tmp file and reload writer + CLI module.

    The CLI imports the writer lazily inside ``run()``, so it picks up the
    reloaded module automatically. We still reload here so any state in
    the writer module (DB_PATH cached at import time) tracks the env var.
    """
    dispatch_db_path = tmp_path / "dispatch.db"
    monkeypatch.setenv("DISPATCH_DB", str(dispatch_db_path))
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)

    from agents import dispatch_db as writer_mod
    importlib.reload(writer_mod)
    writer_mod.init_db()

    from tools.dashboard import backfill_worktree_merges as cli_mod
    importlib.reload(cli_mod)

    return {
        "dispatch_db": dispatch_db_path,
        "writer": writer_mod,
        "cli": cli_mod,
    }


def _read_rows(db_path: Path) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM dispatch_runs ORDER BY completed_at DESC"
        ).fetchall()]
    finally:
        conn.close()


# ── Classification tests ──────────────────────────────────────────────


def test_skips_dispatcher_merge_subjects(repo, isolated_dispatch_env):
    cli = isolated_dispatch_env["cli"]
    _commit(repo, name="a.txt", content="a\n",
            subject="merge: auto-foo — pulled in feature work")

    summary = cli.run(
        since="1970-01-01", repo=repo, do_commit=False,
        out=io.StringIO(),
    )
    candidates = [c for c in summary["classifications"]
                  if "auto-foo" in c.subject]
    assert len(candidates) == 1
    assert candidates[0].kind == "skip:dispatcher-merge"
    assert summary["counts"]["would-write"] == 0


def test_skips_rebase_merge_subjects(repo, isolated_dispatch_env):
    cli = isolated_dispatch_env["cli"]
    _commit(repo, name="b.txt", content="b\n",
            subject="Merge branch 'master' into agent/auto-xfnqh")

    summary = cli.run(
        since="1970-01-01", repo=repo, do_commit=False,
        out=io.StringIO(),
    )
    candidates = [c for c in summary["classifications"]
                  if "agent/auto-xfnqh" in c.subject]
    assert len(candidates) == 1
    assert candidates[0].kind == "skip:rebase-merge"


def test_skips_commits_already_in_dispatch_runs(repo, isolated_dispatch_env):
    cli = isolated_dispatch_env["cli"]
    db_path = isolated_dispatch_env["dispatch_db"]

    sha = _commit(repo, name="c.txt", content="c\n",
                  subject="feat(thing): already known")

    # Pre-seed dispatch_runs with a row carrying this commit_hash. Mirrors
    # the live dispatcher writing its own row before backfill ever runs.
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO dispatch_runs (id, bead_id, status, commit_hash, kind) "
        "VALUES (?, ?, 'DONE', ?, 'bead')",
        ("auto-baz", "auto-baz", sha),
    )
    conn.commit()
    conn.close()

    summary = cli.run(
        since="1970-01-01", repo=repo, do_commit=False,
        out=io.StringIO(),
    )
    by_sha = {c.sha: c for c in summary["classifications"]}
    assert by_sha[sha].kind == "skip:already-recorded"
    assert by_sha[sha].existing_run_id == "auto-baz"


def test_extracts_session_from_subject_token(repo, isolated_dispatch_env):
    cli = isolated_dispatch_env["cli"]
    sha = _commit(repo, name="d.txt", content="d\n",
                  subject="feat(activity): notif tab (auto-r92kc)")

    summary = cli.run(
        since="1970-01-01", repo=repo, do_commit=True,
        out=io.StringIO(),
    )
    by_sha = {c.sha: c for c in summary["classifications"]}
    assert by_sha[sha].kind == "would-write"
    assert by_sha[sha].session_token == "auto-r92kc"

    rows = [r for r in _read_rows(isolated_dispatch_env["dispatch_db"])
            if r["commit_hash"] == sha]
    assert len(rows) == 1
    assert rows[0]["container_name"] == "auto-r92kc"
    assert rows[0]["reason"] == "backfill"
    assert rows[0]["kind"] == "worktree-merge"
    assert rows[0]["bead_id"] is None


def test_handles_subject_with_no_session_token(repo, isolated_dispatch_env):
    cli = isolated_dispatch_env["cli"]
    sha = _commit(repo, name="e.txt", content="e\n",
                  subject="fix(viewer): plain subject no token")

    summary = cli.run(
        since="1970-01-01", repo=repo, do_commit=True,
        out=io.StringIO(),
    )
    by_sha = {c.sha: c for c in summary["classifications"]}
    assert by_sha[sha].kind == "would-write"
    assert by_sha[sha].session_token is None

    rows = [r for r in _read_rows(isolated_dispatch_env["dispatch_db"])
            if r["commit_hash"] == sha]
    assert len(rows) == 1
    assert rows[0]["container_name"] is None


# ── Write-mode tests ──────────────────────────────────────────────────


def test_dry_run_does_not_write(repo, isolated_dispatch_env):
    cli = isolated_dispatch_env["cli"]
    _commit(repo, name="f.txt", content="f\n",
            subject="feat: would-be-written")

    out = io.StringIO()
    cli.run(since="1970-01-01", repo=repo, do_commit=False, out=out)

    rows = _read_rows(isolated_dispatch_env["dispatch_db"])
    assert rows == []
    text = out.getvalue()
    assert "would write" in text
    assert "dry-run" in text


def test_commit_writes_rows(repo, isolated_dispatch_env):
    cli = isolated_dispatch_env["cli"]
    _commit(repo, name="g1.txt", content="g1\n",
            subject="feat: one (auto-aaaaa)")
    _commit(repo, name="g2.txt", content="g2\n",
            subject="feat: two (auto-bbbbb)")
    _commit(repo, name="g3.txt", content="g3\n",
            subject="merge: auto-skipme — should not write")

    cli.run(since="1970-01-01", repo=repo, do_commit=True, out=io.StringIO())

    rows = [r for r in _read_rows(isolated_dispatch_env["dispatch_db"])
            if r["kind"] == "worktree-merge"]
    assert len(rows) == 2
    assert all(r["reason"] == "backfill" for r in rows)
    tokens = sorted(r["container_name"] for r in rows)
    assert tokens == ["auto-aaaaa", "auto-bbbbb"]


def test_idempotent_on_rerun(repo, isolated_dispatch_env):
    cli = isolated_dispatch_env["cli"]
    _commit(repo, name="h.txt", content="h\n",
            subject="feat: idempotent (auto-iiiii)")

    cli.run(since="1970-01-01", repo=repo, do_commit=True, out=io.StringIO())
    first = _read_rows(isolated_dispatch_env["dispatch_db"])
    cli.run(since="1970-01-01", repo=repo, do_commit=True, out=io.StringIO())
    second = _read_rows(isolated_dispatch_env["dispatch_db"])

    assert len(first) == 1
    assert len(second) == 1
    assert first[0]["id"] == second[0]["id"]


# ── Parsing tests ─────────────────────────────────────────────────────


def test_lines_files_extracted_from_shortstat(isolated_dispatch_env):
    """``parse_shortstat`` handles the field-optional shapes git emits.

    Mirrors the dispatcher's ``--shortstat`` invocation
    (agents/dispatcher.py:1310) and tolerates the binary-only / pure-rename
    cases where insertions or deletions may be absent.
    """
    parse = isolated_dispatch_env["cli"].parse_shortstat

    assert parse(" 5 files changed, 142 insertions(+), 8 deletions(-)") == (142, 8, 5)
    assert parse(" 1 file changed, 3 insertions(+)") == (3, 0, 1)
    assert parse(" 2 files changed, 4 deletions(-)") == (0, 4, 2)
    assert parse(" 1 file changed, 1 insertion(+), 1 deletion(-)") == (1, 1, 1)
    assert parse("") == (0, 0, 0)


def test_extract_session_token_handles_edge_cases(isolated_dispatch_env):
    extract = isolated_dispatch_env["cli"].extract_session_token

    assert extract("feat(x): thing (auto-r92kc)") == "auto-r92kc"
    assert extract("merge: auto-foo — reason") is None  # outside parens
    assert extract("plain commit") is None
    assert extract("(auto-1ab2c)") == "auto-1ab2c"


# ── --since filter ────────────────────────────────────────────────────


def test_since_filter_respected(repo, isolated_dispatch_env):
    """Commits older than ``--since`` are excluded from the walk."""
    cli = isolated_dispatch_env["cli"]
    old_sha = _commit(
        repo, name="old.txt", content="old\n",
        subject="feat: ancient (auto-old00)",
        when="2024-01-01T12:00:00Z",
    )
    new_sha = _commit(
        repo, name="new.txt", content="new\n",
        subject="feat: recent (auto-new00)",
        when="2026-04-15T12:00:00Z",
    )

    summary = cli.run(
        since="2026-04-01", repo=repo, do_commit=False, out=io.StringIO(),
    )
    walked_shas = {c.sha for c in summary["classifications"]}
    assert new_sha in walked_shas
    assert old_sha not in walked_shas


# ── Writer integration ────────────────────────────────────────────────


def test_backfill_persists_committer_time_as_completed_at(
    repo, isolated_dispatch_env,
):
    """The row's ``completed_at`` must be the commit's committer time, not
    the wall-clock at backfill time. This is what makes the backfill row
    land in the original merge slot on the timeline.
    """
    cli = isolated_dispatch_env["cli"]
    sha = _commit(
        repo, name="t.txt", content="t\n",
        subject="feat: pinned (auto-time1)",
        when="2026-04-15T12:34:56Z",
    )

    cli.run(since="2026-04-01", repo=repo, do_commit=True, out=io.StringIO())

    rows = [r for r in _read_rows(isolated_dispatch_env["dispatch_db"])
            if r["commit_hash"] == sha]
    assert len(rows) == 1
    completed = datetime.strptime(rows[0]["completed_at"], "%Y-%m-%d %H:%M:%S")
    assert completed == datetime(2026, 4, 15, 12, 34, 56)
    # Backfill rows have duration 0 → started_at == completed_at.
    assert rows[0]["started_at"] == rows[0]["completed_at"]
    assert rows[0]["duration_secs"] == 0
