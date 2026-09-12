from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.graph import worktree_cmd as command


def _row(**overrides) -> dict:
    row = {
        "session_name": "auto-test",
        "repo_name": "autonomy",
        "managed_clone": "/host/repos/autonomy.git",
        "branch": "session/auto-test",
        "target_branch": "master",
        "commits_ahead": 1,
        "dirty_count": 0,
        "is_dirty": False,
        "clone_stale": False,
        "rebase_required": False,
        "ff_eligible": True,
    }
    row.update(overrides)
    return row


def test_resolve_target_uses_path_git_identity(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "checkout"
    root.mkdir()
    monkeypatch.setenv("AUTONOMY_SESSION", "auto-test")

    def fake_git(_path: Path, *args: str) -> str:
        values = {
            ("rev-parse", "--show-toplevel"): str(root),
            ("branch", "--show-current"): "session/auto-test",
            ("rev-parse", "--git-common-dir"): "/host/repos/autonomy.git/.git",
        }
        return values[args]

    monkeypatch.setattr(command, "_git", fake_git)
    monkeypatch.setattr(command, "_api_request", lambda *_: [_row()])

    target = command._resolve_target(str(root))
    assert target.root == root
    assert target.session_name == "auto-test"
    assert target.repo_name == "autonomy"


def test_merge_is_one_sync_rebase_merge_workflow(monkeypatch, tmp_path: Path) -> None:
    target = command.WorktreeTarget(tmp_path, "auto-test", "autonomy", _row())
    monkeypatch.setattr(command, "_resolve_target", lambda _path: target)
    git_calls: list[tuple[str, ...]] = []

    def fake_git(_path: Path, *args: str) -> str:
        git_calls.append(args)
        if args[:1] == ("rev-parse",):
            return "abc123"
        if args[:1] == ("rev-list",):
            return "1\t0"
        return ""

    api_calls: list[tuple[str, str]] = []

    def fake_api(method: str, path: str):
        api_calls.append((method, path))
        if path.endswith("/sync-base"):
            return {"state": _row(rebase_required=True, ff_eligible=False)}
        if path.endswith("/refresh"):
            return _row()
        if path.endswith("/merge"):
            return {"ok": True, "commit": "abc123", "message": "ready"}
        raise AssertionError(path)

    monkeypatch.setattr(command, "_git", fake_git)
    monkeypatch.setattr(command, "_api_request", fake_api)

    command.cmd_worktree_merge(SimpleNamespace(path="."))

    assert ("rebase", "master") in git_calls
    assert api_calls == [
        ("POST", "/api/worktrees/auto-test/autonomy/sync-base"),
        ("POST", "/api/worktrees/auto-test/autonomy/refresh"),
        ("POST", "/api/worktrees/auto-test/autonomy/merge"),
    ]


@pytest.fixture
def checkout(tmp_path):
    import subprocess

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(tmp_path), *args], check=True,
            capture_output=True, text=True,
        ).stdout.strip()

    git("init", "-b", "master")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    git("commit", "--allow-empty", "-m", "base")
    git("checkout", "-b", "session/test")
    return command.WorktreeTarget(tmp_path, "auto-test", "autonomy", _row()), git


def test_sync_fast_forwards_checkout_without_requesting_rebase(checkout, monkeypatch, capsys):
    target, git = checkout
    git("checkout", "master")
    for i in range(3):
        git("commit", "--allow-empty", "-m", f"update {i}")
    git("checkout", "session/test")
    target_head = git("rev-parse", "master")
    monkeypatch.setattr(command, "_resolve_target", lambda _: target)
    monkeypatch.setattr(command, "_api_request", lambda method, path: {
        "state": _row(commits_ahead=0, rebase_required=False),
    } if path.endswith("sync-base") else _row())
    command.cmd_worktree_sync(SimpleNamespace(path="."))
    out = capsys.readouterr().out
    assert "Sync COMPLETE — your checked-out code was updated." in out
    assert "Fast-forward: performed" in out
    assert "0 commit(s) ahead, 0 behind local master" in out
    assert "Rebase:   not performed" in out
    assert "Current:  YES" in out
    assert git("rev-parse", "HEAD") == target_head


def test_sync_fast_forwards_locally_but_reports_stale_host_separately(
    checkout, monkeypatch, capsys,
):
    target, git = checkout
    git("checkout", "master")
    git("commit", "--allow-empty", "-m", "known local update")
    target_head = git("rev-parse", "master")
    git("checkout", "session/test")
    monkeypatch.setattr(command, "_resolve_target", lambda _: target)
    monkeypatch.setattr(command, "_api_request", lambda method, path: {
        "state": _row(commits_ahead=0, rebase_required=False, clone_stale=True),
    } if path.endswith("sync-base") else _row(clone_stale=True))

    command.cmd_worktree_sync(SimpleNamespace(path="."))
    out = capsys.readouterr().out
    assert "Sync COMPLETE — your checked-out code was updated." in out
    assert "Host confirmation INCOMPLETE" in out
    assert "Current:  NOT CONFIRMED against the host" in out
    assert git("rev-parse", "HEAD") == target_head


@pytest.mark.parametrize("ahead,behind,stale,expected", [
    (0, 0, False, "Current:  YES"),
    (2, 0, False, "Current:  YES"),
    (2, 3, False, "Merge:    needs rebase"),
    (0, 0, True, "Current:  NOT CONFIRMED"),
    (0, 0, None, "Host base: UNKNOWN"),
])
def test_status_distinguishes_checkout_distance_and_host_freshness(
    tmp_path, capsys, ahead, behind, stale, expected,
):
    target = command.WorktreeTarget(tmp_path, "auto-test", "autonomy", _row())
    row = _row(commits_ahead=ahead, commits_behind=behind, clone_stale=stale,
               checkout_commit="abc", base_commit="def")
    command._print_status(target, row)
    out = capsys.readouterr().out
    assert f"{ahead} commit(s) ahead, {behind} behind local master" in out
    assert expected in out


def test_checkout_measures_dirty_files_without_calling_them_behind(checkout, capsys):
    target, _ = checkout
    (target.root / "uncommitted.txt").write_text("work")
    row = command._checkout_state(target, _row())
    command._print_status(target, row)
    out = capsys.readouterr().out
    assert "Files:    dirty (1 paths)" in out
    assert "0 commit(s) ahead, 0 behind" in out
    assert "Current:  YES" in out
    assert "Merge:    uncommitted changes; commit or stash before merging" in out


def test_host_prune_refuses_to_run_inside_session(monkeypatch, capsys) -> None:
    monkeypatch.setenv("AUTONOMY_SESSION", "auto-test")
    monkeypatch.setattr(
        command,
        "cleanup_session_worktrees",
        lambda *_args, **_kwargs: pytest.fail("cleanup must not run"),
    )
    args = SimpleNamespace(
        worktrees_dir=None,
        force=False,
        force_all=False,
        session=None,
    )

    with pytest.raises(SystemExit) as raised:
        command.cmd_worktree_host_prune(args)

    assert raised.value.code == 2
    assert "cannot run inside a session" in capsys.readouterr().err


def test_host_prune_refuses_a_live_target(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.delenv("AUTONOMY_SESSION", raising=False)
    monkeypatch.delenv("GRAPH_SESSION", raising=False)
    monkeypatch.delenv("BD_ACTOR", raising=False)
    monkeypatch.delenv("CROSSTALK_TOKEN", raising=False)
    monkeypatch.setattr(command, "_get_live_session_names", lambda: ["auto-live"])
    args = SimpleNamespace(
        worktrees_dir=str(tmp_path),
        force=False,
        force_all=False,
        session="auto-live",
    )

    with pytest.raises(SystemExit) as raised:
        command.cmd_worktree_host_prune(args)

    assert raised.value.code == 2
    assert "refusing to prune live session" in capsys.readouterr().err


def test_host_prune_refuses_when_liveness_cannot_be_checked(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    monkeypatch.delenv("AUTONOMY_SESSION", raising=False)
    monkeypatch.delenv("GRAPH_SESSION", raising=False)
    monkeypatch.delenv("BD_ACTOR", raising=False)
    monkeypatch.delenv("CROSSTALK_TOKEN", raising=False)
    monkeypatch.setattr(command, "_get_live_session_names", lambda: None)
    args = SimpleNamespace(
        worktrees_dir=str(tmp_path),
        force=False,
        force_all=False,
        session="auto-ended",
    )

    with pytest.raises(SystemExit) as raised:
        command.cmd_worktree_host_prune(args)

    assert raised.value.code == 2
    assert "cannot verify that the target session has ended" in capsys.readouterr().err
