from __future__ import annotations

from types import SimpleNamespace

from tools.graph import dispatch_cmd


def test_cmd_dispatch_reset_uses_dashboard_api_in_container_mode(monkeypatch, capsys):
    monkeypatch.setenv("GRAPH_API", "https://localhost:8080")

    calls: dict[str, object] = {}

    def fake_api_post(base_url, path, ctx, body=None):
        calls["base_url"] = base_url
        calls["path"] = path
        calls["body"] = body
        return {
            "bead_id": "auto-edec1.1",
            "reset": True,
            "agent_failures": 3,
            "merge_failures": 0,
            "run_id": "reset-auto-edec1.1-deadbeef",
            "agent_failures_after": 0,
            "merge_failures_after": 0,
        }

    monkeypatch.setattr(dispatch_cmd, "_api_post", fake_api_post)

    run_calls: list[list[str]] = []

    class _Completed:
        returncode = 0
        stderr = ""

    def fake_run(argv, capture_output=True, text=True):
        run_calls.append(argv)
        return _Completed()

    monkeypatch.setattr(dispatch_cmd.subprocess, "run", fake_run)

    dispatch_cmd.cmd_dispatch_reset(SimpleNamespace(bead_id="auto-edec1.1"))

    out = capsys.readouterr().out
    assert calls["path"] == "/api/dispatch/reset/auto-edec1.1"
    assert run_calls == [["bd", "set-state", "auto-edec1.1", "readiness=approved"]]
    assert "Inserted synthetic DONE record" in out
    assert "re-approved for dispatch" in out

