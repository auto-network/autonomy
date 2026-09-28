"""Bring a remote session's branch home (graph://7eb29bc8-31a §6.5, bead
auto-yi2pe part b): the far side bundles ``session/<name>`` above the
commits Home says it has; Home fetches it into its managed clone as
``session/<name>@<machine>`` and checks it out where Worktrees lists it."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from tools.dashboard import server
from tools.network import session_control

PEER = "a1" * 32


def git(cwd, *args):
    out = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def commit(repo, name, text):
    (Path(repo) / name).write_text(text)
    git(repo, "add", name)
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", name)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def machines(tmp_path, monkeypatch):
    """origin -> an SJC clone with a session worktree, and a Home clone."""
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "master")
    base = commit(origin, "base.txt", "base")
    sjc_clone = tmp_path / "sjc" / "autonomy.git"
    git(tmp_path, "clone", "-q", "--bare", str(origin), str(sjc_clone))
    home_clone = tmp_path / "home" / "autonomy.git"
    git(tmp_path, "clone", "-q", "--bare", str(origin), str(home_clone))
    sjc_worktrees = tmp_path / "sjc" / "worktrees"
    wt = sjc_worktrees / "auto-9" / "autonomy-auto-9"
    wt.parent.mkdir(parents=True)
    git(sjc_clone, "worktree", "add", "-q", "-b", "session/auto-9", str(wt), "master")
    head = commit(wt, "work.txt", "remote work")
    monkeypatch.setattr(server, "WORKTREES_DIR", sjc_worktrees)
    monkeypatch.setattr(session_control, "_data_root", lambda: tmp_path / "data")
    return {"base": base, "head": head, "home_clone": home_clone,
            "tmp": tmp_path, "sjc_worktrees": sjc_worktrees}


def _fetch(body):
    return asyncio.run(server._inbound_session_fetch_branch(body, PEER))


def test_the_branch_is_bundled_above_home_base_and_imported_into_worktrees(
        machines, monkeypatch):
    reply = _fetch({"tmux_name": "auto-9", "repo": "autonomy", "have": [machines["base"]]})
    assert reply["ok"] is True, reply
    result = reply["result"]
    assert result["head"] == machines["head"] and result["stream_delete"] is True
    bundle = Path(result["stream_file"])
    assert bundle.parent == machines["tmp"] / "data" / "session-transfer"

    home_worktrees = machines["tmp"] / "home" / "worktrees"
    monkeypatch.setattr(server, "WORKTREES_DIR", home_worktrees)
    imported = server._import_remote_branch(
        machines["home_clone"], bundle, result["branch"], result["head"],
        "auto-9@sjc-2", "autonomy")
    assert imported["head"] == machines["head"], imported
    assert git(machines["home_clone"], "rev-parse", "session/auto-9@sjc-2") == machines["head"]
    worktree = home_worktrees / "auto-9@sjc-2" / "autonomy-auto-9@sjc-2"
    assert (worktree / "work.txt").read_text() == "remote work"


def test_nothing_new_is_an_empty_answer(machines):
    reply = _fetch({"tmux_name": "auto-9", "repo": "autonomy", "have": [machines["head"]]})
    assert reply["ok"] is True and reply["result"]["empty"] is True
    assert "stream_file" not in reply["result"]


def test_an_unknown_base_is_refused_rather_than_bundling_everything(machines):
    reply = _fetch({"tmux_name": "auto-9", "repo": "autonomy", "have": ["f" * 40]})
    assert reply["refusal"] == "base-missing"


@pytest.mark.parametrize("body,refusal", [
    ({"tmux_name": "auto-nope", "repo": "autonomy", "have": ["a" * 40]}, "no-such-session"),
    ({"tmux_name": "auto-9", "repo": "../x", "have": ["a" * 40]}, "bad-request"),
    ({"tmux_name": "auto-9", "repo": "autonomy", "have": []}, "bad-request"),
    ({"tmux_name": "auto-9", "repo": "autonomy", "have": ["HEAD"]}, "bad-request"),
])
def test_fetch_branch_refusals(machines, body, refusal):
    assert _fetch(body)["refusal"] == refusal


def test_a_refetch_updates_a_clean_worktree_and_spares_a_dirty_one(machines, monkeypatch):
    first = _fetch({"tmux_name": "auto-9", "repo": "autonomy", "have": [machines["base"]]})
    home_worktrees = machines["tmp"] / "home" / "worktrees"
    monkeypatch.setattr(server, "WORKTREES_DIR", home_worktrees)
    server._import_remote_branch(
        machines["home_clone"], Path(first["result"]["stream_file"]),
        "session/auto-9", machines["head"], "auto-9@sjc-2", "autonomy")

    sjc_wt = machines["sjc_worktrees"] / "auto-9" / "autonomy-auto-9"
    monkeypatch.setattr(server, "WORKTREES_DIR", machines["sjc_worktrees"])
    newer = commit(sjc_wt, "more.txt", "more")
    second = _fetch({"tmux_name": "auto-9", "repo": "autonomy", "have": [machines["head"]]})
    monkeypatch.setattr(server, "WORKTREES_DIR", home_worktrees)
    updated = server._import_remote_branch(
        machines["home_clone"], Path(second["result"]["stream_file"]),
        "session/auto-9", newer, "auto-9@sjc-2", "autonomy")
    worktree = home_worktrees / "auto-9@sjc-2" / "autonomy-auto-9@sjc-2"
    assert updated["head"] == newer and (worktree / "more.txt").exists()

    # A worktree with the operator's uncommitted changes is left alone.
    (worktree / "local.txt").write_text("operator edit")
    git(worktree, "add", "local.txt")
    monkeypatch.setattr(server, "WORKTREES_DIR", machines["sjc_worktrees"])
    latest = commit(sjc_wt, "fourth.txt", "fourth")
    fourth = _fetch({"tmux_name": "auto-9", "repo": "autonomy", "have": [newer]})
    monkeypatch.setattr(server, "WORKTREES_DIR", home_worktrees)
    spared = server._import_remote_branch(
        machines["home_clone"], Path(fourth["result"]["stream_file"]),
        "session/auto-9", latest, "auto-9@sjc-2", "autonomy")
    assert "local changes" in spared["warning"]
    assert (worktree / "local.txt").read_text() == "operator edit"



# ── the route ───────────────────────────────────────────────────────────────


class _Req:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


@pytest.fixture
def route(machines, monkeypatch):
    from types import SimpleNamespace

    from tools.dashboard import api_auth, session_presence
    from tools.dashboard import session_control_client as scc

    monkeypatch.setattr(api_auth, "require_global_api_authority", lambda request: None)
    monkeypatch.setattr(session_presence, "read_presence", lambda: [{
        "tmux_name": "auto-9", "machine": "sjc-2", "machine_pub": PEER,
        "project": "autonomy-developer-opus", "local": False, "reachable": True}])
    monkeypatch.setattr(scc, "resolve_machine", lambda name: PEER if name == "sjc-2" else None)
    repo = SimpleNamespace(url="https://github.com/o/autonomy.git", writable=True)
    monkeypatch.setattr(server.workspace_settings, "get_workspace",
                        lambda project: SimpleNamespace(repos=[repo]))
    import agents.workspace_manager as wm
    monkeypatch.setattr(wm, "managed_clone_path", lambda url: machines["home_clone"])
    state = SimpleNamespace(asked=[], reply={
        "v": 1, "ok": True, "result": {"head": machines["base"], "empty": True}})

    async def fake_request(machine, op, body=None, *, timeout=15.0, stream=False):
        state.asked.append((machine, op, body, stream))
        return state.reply

    monkeypatch.setattr(scc, "request", fake_request)
    return state


def _call(body):
    import json

    response = asyncio.run(server.api_worktree_remote_fetch(_Req(body)))
    return response.status_code, json.loads(response.body)


def test_the_route_asks_with_this_clones_base_and_reports_nothing_new(route, machines):
    status, data = _call({"session": "auto-9@sjc-2"})
    assert status == 200, data
    assert data == {"session": f"auto-9@{PEER[:12]}", "display": "auto-9@sjc-2",
                    "repos": [{"repo": "autonomy", "head": machines["base"],
                               "empty": True}]}
    ((machine, op, body, stream),) = route.asked
    assert (machine, op, stream) == ("sjc-2", "fetch-branch", True)
    assert body == {"tmux_name": "auto-9", "repo": "autonomy", "have": [machines["base"]]}


def test_the_route_rejects_a_bad_address_and_an_unknown_session(route):
    assert _call({"session": "auto-9"})[0] == 400
    assert _call({"session": "auto-1@sjc-2"})[0] == 404
    assert route.asked == []


def test_a_refusal_is_reported_per_repo(route):
    route.reply = {"v": 1, "ok": False, "refusal": "base-missing", "detail": "x"}
    status, data = _call({"session": "auto-9@sjc-2"})
    assert status == 409
    assert data["repos"][0]["refusal"] == "base-missing"



@pytest.mark.parametrize("label", ["a/../../b", "Home Mac", "x~^:y"])
def test_a_free_text_machine_label_never_becomes_a_path_or_ref(
        route, machines, monkeypatch, label):
    """The profile label is display only: the import lands under the durable
    key's prefix, inside data/worktrees, on a valid ref."""
    from tools.dashboard import session_presence

    monkeypatch.setattr(session_presence, "read_presence", lambda: [{
        "tmux_name": "auto-9", "machine": label, "machine_pub": PEER,
        "project": "autonomy-developer-opus", "local": False, "reachable": True}])
    home_worktrees = machines["tmp"] / "home" / "worktrees"
    monkeypatch.setattr(server, "WORKTREES_DIR", machines["sjc_worktrees"])
    real = _fetch({"tmux_name": "auto-9", "repo": "autonomy", "have": [machines["base"]]})
    monkeypatch.setattr(server, "WORKTREES_DIR", home_worktrees)
    # What the requesting connector hands the dashboard: the streamed copy
    # under result.file (tools.network.session_control._receive_stream).
    route.reply = {**real, "result": {**real["result"],
                                      "file": real["result"]["stream_file"]}}
    status, data = _call({"session": "auto-9@sjc-2"})
    assert status == 200, data
    assert data["session"] == f"auto-9@{PEER[:12]}"
    assert data["display"] == f"auto-9@{label}"
    worktree = Path(data["repos"][0]["worktree"]).resolve()
    worktree.relative_to(home_worktrees.resolve())
    assert git(machines["home_clone"], "rev-parse",
               f"session/auto-9@{PEER[:12]}") == machines["head"]


def test_the_worktree_follows_what_the_bundle_delivered_not_the_claim(machines, monkeypatch):
    reply = _fetch({"tmux_name": "auto-9", "repo": "autonomy", "have": [machines["base"]]})
    monkeypatch.setattr(server, "WORKTREES_DIR", machines["tmp"] / "home" / "worktrees")
    out = server._import_remote_branch(
        machines["home_clone"], Path(reply["result"]["stream_file"]),
        "session/auto-9", machines["base"], "auto-9@a1a1a1a1a1a1", "autonomy")
    assert out["head"] == machines["head"]
    assert "peer claimed head" in out["warning"]


def test_an_untracked_file_counts_as_local_changes(machines, monkeypatch):
    first = _fetch({"tmux_name": "auto-9", "repo": "autonomy", "have": [machines["base"]]})
    home_worktrees = machines["tmp"] / "home" / "worktrees"
    monkeypatch.setattr(server, "WORKTREES_DIR", home_worktrees)
    server._import_remote_branch(
        machines["home_clone"], Path(first["result"]["stream_file"]),
        "session/auto-9", machines["head"], "auto-9@a1a1a1a1a1a1", "autonomy")
    worktree = home_worktrees / "auto-9@a1a1a1a1a1a1" / "autonomy-auto-9@a1a1a1a1a1a1"
    (worktree / "scratch.txt").write_text("operator scratch")
    sjc_wt = machines["sjc_worktrees"] / "auto-9" / "autonomy-auto-9"
    monkeypatch.setattr(server, "WORKTREES_DIR", machines["sjc_worktrees"])
    newer = commit(sjc_wt, "scratch.txt", "remote version")
    second = _fetch({"tmux_name": "auto-9", "repo": "autonomy", "have": [machines["head"]]})
    monkeypatch.setattr(server, "WORKTREES_DIR", home_worktrees)
    spared = server._import_remote_branch(
        machines["home_clone"], Path(second["result"]["stream_file"]),
        "session/auto-9", newer, "auto-9@a1a1a1a1a1a1", "autonomy")
    assert "local changes" in spared["warning"]
    assert (worktree / "scratch.txt").read_text() == "operator scratch"
