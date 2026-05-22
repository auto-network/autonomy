"""L1 tests for the /api/session/{tmux}/background endpoint.

The dashboard's Claude harness sessions render a Ctrl-B button next to the
existing Esc interrupt button. Pressing Ctrl-B sends a literal Ctrl-B to the
tmux pane, which the Claude harness interprets as "background the running
tool" rather than cancelling it (which is what Escape does).
"""
from unittest.mock import patch


def test_background_sends_ctrl_b_to_existing_tmux(test_client):
    from tools.dashboard import server as server_mod

    with patch.object(server_mod, "_tmux_session_exists", return_value=True), \
         patch.object(server_mod.subprocess, "run") as mock_run:
        resp = test_client.post("/api/session/auto-test-0001/background")

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    # The endpoint must invoke `tmux send-keys -t <name> C-b` exactly once.
    send_keys_calls = [
        call.args[0] for call in mock_run.call_args_list
        if len(call.args) >= 1
        and isinstance(call.args[0], list)
        and call.args[0][:2] == ["tmux", "send-keys"]
    ]
    assert send_keys_calls == [
        ["tmux", "send-keys", "-t", "auto-test-0001", "C-b"]
    ], send_keys_calls


def _send_keys_calls(mock_run):
    return [
        call.args[0] for call in mock_run.call_args_list
        if len(call.args) >= 1
        and isinstance(call.args[0], list)
        and call.args[0][:2] == ["tmux", "send-keys"]
    ]


def test_background_returns_404_when_tmux_session_missing(test_client):
    from tools.dashboard import server as server_mod

    with patch.object(server_mod, "_tmux_session_exists", return_value=False), \
         patch.object(server_mod.subprocess, "run") as mock_run:
        resp = test_client.post("/api/session/auto-missing/background")

    assert resp.status_code == 404
    assert _send_keys_calls(mock_run) == []


def test_background_returns_503_when_tmux_unavailable(test_client):
    from tools.dashboard import server as server_mod

    def raise_fnf(_name):
        raise FileNotFoundError("tmux not installed")

    with patch.object(server_mod, "_tmux_session_exists", side_effect=raise_fnf), \
         patch.object(server_mod.subprocess, "run") as mock_run:
        resp = test_client.post("/api/session/auto-test/background")

    assert resp.status_code == 503
    assert _send_keys_calls(mock_run) == []
