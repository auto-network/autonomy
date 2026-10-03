"""graph session-auth re-uses a live approved session instead of asking again."""

import io
import json
import types
import urllib.request

import pytest

from tools.graph import cli


def _jar(path, value="live-cookie", exp=4102444800):
    path.write_text("# Netscape HTTP Cookie File\n"
                    f"#HttpOnly_dash\tFALSE\t/\tTRUE\t{exp}\t"
                    f"{cli._SESSION_AUTH_COOKIE}\t{value}\n")


@pytest.fixture
def calls(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "_resolve_crosstalk_token", lambda: "tok")
    monkeypatch.setattr(cli, "_get_session_name", lambda: "auto-1")
    monkeypatch.setenv("GRAPH_API", "https://dash:8080")
    state = {"unlocked": True, "expires_at": 4102444800}

    def urlopen(req, timeout=None, context=None):
        seen.append((req.get_method(), req.full_url, req.get_header("Cookie")))
        if req.full_url.endswith("/api/identity/session"):
            body = {"unlocked": state["unlocked"],
                    "expires_at": state["expires_at"] if state["unlocked"] else None}
            return io.BytesIO(json.dumps(body).encode())
        raise SystemExit("asked for a new approval")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return seen, state


def _args(jar):
    return types.SimpleNamespace(jar=str(jar), browser=False, wait=5)


def test_live_session_is_reused_without_a_new_approval(tmp_path, calls, capsys):
    seen, _ = calls
    _jar(tmp_path / "jar")
    cli.cmd_session_auth(_args(tmp_path / "jar"))
    assert seen == [("GET", "https://dash:8080/api/identity/session",
                     f"{cli._SESSION_AUTH_COOKIE}=live-cookie")]
    assert "still granted to auto-1" in capsys.readouterr().out


def test_ended_session_asks_the_operator_again(tmp_path, calls):
    seen, state = calls
    state["unlocked"] = False
    _jar(tmp_path / "jar")
    with pytest.raises(SystemExit, match="asked for a new approval"):
        cli.cmd_session_auth(_args(tmp_path / "jar"))
    assert seen[-1][:2] == ("POST", "https://dash:8080/api/approvals")


def test_no_jar_asks_the_operator(tmp_path, calls):
    seen, _ = calls
    with pytest.raises(SystemExit, match="asked for a new approval"):
        cli.cmd_session_auth(_args(tmp_path / "missing"))
    assert [m for m, *_ in seen] == ["POST"]


def test_session_about_to_end_asks_for_a_fresh_one(tmp_path, calls):
    import time
    seen, state = calls
    state["expires_at"] = int(time.time()) + 120
    _jar(tmp_path / "jar")
    with pytest.raises(SystemExit, match="asked for a new approval"):
        cli.cmd_session_auth(_args(tmp_path / "jar"))
    assert seen[-1][:2] == ("POST", "https://dash:8080/api/approvals")
