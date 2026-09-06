"""Tests for the combined session-worktree commit-msg hook.

The hook has two self-gated steps: Signed-off-by (policy config) and the
Autonomy-Provenance trailer (session worktree). These tests exercise the
installer's clobber rules and the hook's stamping behavior end-to-end with
real ``git commit`` runs — including the offline degradation path, since a
commit must never be blocked or left unstamped just because the dashboard
is unreachable.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from agents import workspace_manager as wm


def _git(repo, *args, env_overrides=None):
    env = dict(os.environ)
    # The container test env carries real session credentials; strip them so
    # each test opts into exactly the identity it is exercising.
    env.pop("CROSSTALK_TOKEN", None)
    env.pop("AUTONOMY_SESSION", None)
    env.pop("GRAPH_API", None)
    if env_overrides:
        env.update(env_overrides)
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.fixture
def session_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "session/test-sess-1234")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@local")
    return repo


def _install_hook(repo):
    assert wm._install_commit_msg_hook(repo) is True
    return repo / ".git" / "hooks" / "commit-msg"


def _commit_file(repo, name, message, env_overrides=None):
    (repo / name).write_text(name)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", message, env_overrides=env_overrides)
    return _git(repo, "log", "-1", "--pretty=%B")


# ── installer clobber rules ────────────────────────────────────────

def test_install_writes_hook_and_is_idempotent(session_repo):
    hook = _install_hook(session_repo)
    assert wm._COMMIT_MSG_HOOK_MARKER in hook.read_text()
    assert os.access(hook, os.X_OK)
    assert wm._install_commit_msg_hook(session_repo) is True


def test_install_overwrites_legacy_signoff_hook(session_repo):
    hook = session_repo / ".git" / "hooks" / "commit-msg"
    hook.write_text(f"#!/bin/sh\n{wm._LEGACY_SIGNOFF_HOOK_MARKER}\nexit 0\n")
    assert wm._install_commit_msg_hook(session_repo) is True
    assert wm._COMMIT_MSG_HOOK_MARKER in hook.read_text()


def test_install_refuses_foreign_hook(session_repo):
    hook = session_repo / ".git" / "hooks" / "commit-msg"
    foreign = "#!/bin/sh\n# repo-owned hook\nexit 0\n"
    hook.write_text(foreign)
    assert wm._install_commit_msg_hook(session_repo) is False
    assert hook.read_text() == foreign


# ── stamping behavior ──────────────────────────────────────────────

def test_offline_commit_gets_branch_derived_fallback_stamp(session_repo):
    _install_hook(session_repo)
    body = _commit_file(session_repo, "a", "change a")
    assert "Autonomy-Provenance: autonomy://-/test-sess-1234/-" in body


def test_autonomy_session_env_wins_over_branch(session_repo):
    _install_hook(session_repo)
    body = _commit_file(
        session_repo, "a", "change a",
        env_overrides={"AUTONOMY_SESSION": "auto-env-name"},
    )
    assert "Autonomy-Provenance: autonomy://-/auto-env-name/-" in body


def test_amend_does_not_duplicate_trailer(session_repo):
    _install_hook(session_repo)
    _commit_file(session_repo, "a", "change a")
    _git(session_repo, "commit", "-q", "--amend", "--no-edit")
    body = _git(session_repo, "log", "-1", "--pretty=%B")
    assert body.count("Autonomy-Provenance:") == 1


def test_non_session_branch_is_not_stamped(session_repo):
    _install_hook(session_repo)
    _git(session_repo, "checkout", "-q", "-b", "feature/x")
    body = _commit_file(session_repo, "a", "change a")
    assert "Autonomy-Provenance" not in body


def test_signoff_and_provenance_compose(session_repo):
    _install_hook(session_repo)
    _git(session_repo, "config", "autonomy.sign.requireSignoff", "true")
    body = _commit_file(session_repo, "a", "change a")
    assert "Signed-off-by: Test <test@local>" in body
    assert "Autonomy-Provenance: autonomy://-/test-sess-1234/-" in body


def test_dashboard_locator_is_used_when_reachable(session_repo, tmp_path):
    """The hook consumes the dashboard's minted locator verbatim."""
    import http.server
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path == "/api/session/provenance-stamp?format=locator"
            if self.headers.get("Authorization") == "Bearer tok123":
                payload = b"autonomy://PERSONAKEY/auto-x/42\n"
            else:
                payload = b"unauthorized"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        _install_hook(session_repo)
        body = _commit_file(
            session_repo, "a", "change a",
            env_overrides={
                "CROSSTALK_TOKEN": "tok123",
                "GRAPH_API": f"http://127.0.0.1:{server.server_address[1]}",
            },
        )
        assert "Autonomy-Provenance: autonomy://PERSONAKEY/auto-x/42" in body
    finally:
        server.shutdown()


def test_garbage_dashboard_response_degrades_to_fallback(session_repo):
    """A non-locator response (error page, junk) must never land in a commit."""
    import http.server
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            payload = b"<html>500 Internal Server Error</html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        _install_hook(session_repo)
        body = _commit_file(
            session_repo, "a", "change a",
            env_overrides={
                "CROSSTALK_TOKEN": "tok123",
                "GRAPH_API": f"http://127.0.0.1:{server.server_address[1]}",
            },
        )
        assert "Autonomy-Provenance: autonomy://-/test-sess-1234/-" in body
    finally:
        server.shutdown()
