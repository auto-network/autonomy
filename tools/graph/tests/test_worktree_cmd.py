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
