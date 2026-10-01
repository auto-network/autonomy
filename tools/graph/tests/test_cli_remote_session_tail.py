"""graph tail / context / read for a session on another fleet machine
(auto-x6iel): <tmux name>@<machine> goes to the dashboard's remote tail;
the note-version suffix (f6c6c43e@1, <name>@3) stays local."""

from __future__ import annotations

import argparse

import pytest

from tools.graph import cli

PUB = "b1" * 32
ROW = {"session_id": f"auto-0930-202034@{PUB}", "project": "autonomy-developer-opus",
       "machine": "SJC", "machine_pub": PUB}
ENTRIES = [
    {"type": "user", "content": "first question", "timestamp": "t1"},
    {"type": "tool_use", "content": "ls"},
    {"type": "assistant_text", "content": "first answer", "timestamp": "t2"},
    {"type": "crosstalk", "content": "a peer says hi", "sender": "host-1", "timestamp": "t3"},
    {"type": "tool_result", "content": "files"},
    {"type": "assistant_text", "content": "second answer", "timestamp": "t4"},
]


@pytest.mark.parametrize("value, expected", [
    ("auto-0930-202034@SJC", ("auto-0930-202034", "SJC")),
    (f"auto-0930-202034@{PUB}", ("auto-0930-202034", PUB)),
    ("host-0930-044707@sjc-2", ("host-0930-044707", "sjc-2")),
    ("f6c6c43e@1", None),              # a note version
    ("f6c6c43e-24a@", None),           # a note's version list
    ("auto-0930-202034@3", None),      # a version of the session's source
    ("auto-0930-202034", None),
    ("", None),
])
def test_only_a_tmux_name_at_a_machine_is_remote(value, expected):
    assert cli._remote_session_address(value) == expected


@pytest.fixture
def dashboard(monkeypatch):
    calls = []
    tail = {"entries": ENTRIES}

    def fake(method, path, payload=None):
        calls.append(path)
        if path == "/api/sessions/presence":
            return {"sessions": [ROW, {**ROW, "session_id": f"auto-1@{PUB}"}]}
        return tail

    monkeypatch.setattr(cli, "_dashboard_json", fake)
    monkeypatch.setattr(cli, "_remote_tail_get", lambda path: fake("GET", path))
    return calls, tail


@pytest.mark.parametrize("machine", ["SJC", "sjc", PUB, PUB[:12]])
def test_tail_prints_the_last_turns_by_display_name_or_key(dashboard, capsys, machine):
    calls, _ = dashboard
    args = argparse.Namespace(source=f"auto-0930-202034@{machine}", n=2, max_chars=0,
                              turn=None, window=0)
    cli.cmd_tail(args)
    out = capsys.readouterr().out
    # Always the key form on the wire, whatever was typed.
    assert calls[-1] == (f"/api/session/autonomy-developer-opus/"
                         f"auto-0930-202034%40{PUB}/tail?tail_entries=100")
    assert "## CROSSTALK from host-1 · t3\na peer says hi" in out
    assert "## ASSISTANT · t4\nsecond answer" in out
    assert "first answer" not in out and "files" not in out
    assert "auto-0930-202034@SJC [" in out


def test_context_last_reads_the_same_way(dashboard, capsys):
    cli.cmd_context(argparse.Namespace(source="auto-0930-202034@sjc", turn="last:1",
                                       window=0, max_chars=0))
    assert "second answer" in capsys.readouterr().out


def test_a_session_not_on_any_machine_is_one_line(dashboard, capsys):
    with pytest.raises(SystemExit) as exit_:
        cli.cmd_tail(argparse.Namespace(source="auto-9999-999999@SJC", n=5,
                                        max_chars=0, turn=None, window=0))
    assert exit_.value.code == 1
    err = capsys.readouterr().err
    assert "no such session on another fleet machine" in err
    assert "Traceback" not in err


@pytest.mark.parametrize("flag, words", [
    ("machine_unreachable", "SJC unreachable"),
    ("machine_timed_out", "SJC did not answer in time (reply-timeout)"),
    ("machine_not_enabled", "SJC has remote sessions not enabled"),
])
def test_a_machine_that_does_not_answer_is_one_line(dashboard, capsys, flag, words):
    _calls, tail = dashboard
    tail.clear()
    tail.update({"entries": [], flag: {"machine": "SJC-2", "reason": "reply-timeout"}
                 if flag == "machine_timed_out" else {"machine": "SJC-2"}})
    with pytest.raises(SystemExit):
        cli.cmd_tail(argparse.Namespace(source="auto-0930-202034@SJC", n=5,
                                        max_chars=0, turn=None, window=0))
    assert words in capsys.readouterr().err


def test_read_of_a_note_version_is_not_sent_remote(monkeypatch):
    monkeypatch.setattr(cli, "_dashboard_json",
                        lambda *a, **k: pytest.fail("a note version went to the remote tail"))
    assert cli._remote_session_tail("f6c6c43e@1", None, None) is False


@pytest.mark.parametrize("turn, message", [
    ("last:abc", "Invalid 'last:N' value: 'last:abc'"),
    ("last:0", "'last:N' requires N >= 1"),
    ("last:-3", "'last:N' requires N >= 1"),
])
def test_a_bad_last_n_is_the_local_paths_error_not_every_turn(dashboard, capsys, turn, message):
    calls, _ = dashboard
    cli.cmd_context(argparse.Namespace(source="auto-0930-202034@SJC", turn=turn,
                                       window=0, max_chars=0))
    captured = capsys.readouterr()
    assert message in captured.err
    assert "first answer" not in captured.out and calls == []


def test_the_cli_outwaits_the_dashboards_own_timeout():
    """The dashboard answers machine_timed_out after 20 s; at 15 s the CLI
    gave up first, with a traceback (auto-x6iel live check)."""
    assert cli.REMOTE_TAIL_TIMEOUT_S > 20


@pytest.mark.parametrize("exc, words", [
    (TimeoutError("The read operation timed out"), "did not answer within 45 s (TimeoutError)"),
    (None, "Cannot reach dashboard"),
])
def test_a_dashboard_that_does_not_answer_is_one_line(monkeypatch, capsys, exc, words):
    import urllib.error
    import urllib.request

    def urlopen(*a, **k):
        raise exc if exc is not None else urllib.error.URLError("refused")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(cli, "_resolve_crosstalk_token", lambda: "t")
    with pytest.raises(SystemExit) as exit_:
        cli._remote_tail_get("/api/session/p/x/tail")
    assert exit_.value.code == 1
    err = capsys.readouterr().err
    assert words in err and "Traceback" not in err
