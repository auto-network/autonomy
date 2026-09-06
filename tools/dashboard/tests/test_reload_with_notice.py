from types import SimpleNamespace

from tools.dashboard import reload_with_notice


class _Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_reload_notice_posts_token_to_running_dashboard(monkeypatch):
    seen = {}
    monkeypatch.setenv("DASHBOARD_RESTART_TOKEN", "shared-token")

    def urlopen(req, *, timeout, context):
        seen["url"] = req.full_url
        seen["token"] = req.headers["X-dashboard-restart-token"]
        seen["timeout"] = timeout
        seen["context"] = context
        seen["body"] = req.data
        return _Response()

    monkeypatch.setattr(reload_with_notice.request, "urlopen", urlopen)
    assert reload_with_notice._notify_dashboard(
        SimpleNamespace(port=8080, ssl_certfile=None),
        ["/app/tools/dashboard/server.py"],
    )
    assert seen["url"] == "http://127.0.0.1:8080/api/internal/restart-notice"
    assert seen["token"] == "shared-token"
    assert seen["timeout"] == 1
    assert seen["context"] is None
    import json
    assert json.loads(seen["body"]) == {
        "changed_files": ["/app/tools/dashboard/server.py"],
    }


def test_notify_dashboard_sends_empty_list_when_no_changes(monkeypatch):
    seen = {}
    monkeypatch.setenv("DASHBOARD_RESTART_TOKEN", "shared-token")

    def urlopen(req, *, timeout, context):
        seen["body"] = req.data
        return _Response()

    monkeypatch.setattr(reload_with_notice.request, "urlopen", urlopen)
    assert reload_with_notice._notify_dashboard(SimpleNamespace(port=8080, ssl_certfile=None))
    import json
    assert json.loads(seen["body"]) == {"changed_files": []}


def test_reload_waits_only_after_dashboard_accepts_notice(monkeypatch):
    calls = []
    supervisor = SimpleNamespace(config=SimpleNamespace(port=8080, ssl_certfile=None))
    supervisor._last_changed_paths = ["/app/server.py"]
    monkeypatch.setattr(
        reload_with_notice, "_notify_dashboard",
        lambda _config, changed=None: calls.append(("notify", changed)) or True)
    monkeypatch.setattr(reload_with_notice.time, "sleep", lambda seconds: calls.append(("sleep", seconds)))
    monkeypatch.setattr(reload_with_notice, "_original_restart", lambda _self: calls.append(("restart", None)))

    reload_with_notice._restart_with_notice(supervisor)

    assert calls == [("notify", ["/app/server.py"]), ("sleep", 3), ("restart", None)]


def test_next_capturing_stashes_changed_paths(monkeypatch):
    from pathlib import Path

    captured = SimpleNamespace()
    monkeypatch.setattr(
        reload_with_notice, "_original_next",
        lambda self: [Path("/app/server.py")])
    result = reload_with_notice._next_capturing(captured)
    assert result == [Path("/app/server.py")]
    assert captured._last_changed_paths == ["/app/server.py"]
