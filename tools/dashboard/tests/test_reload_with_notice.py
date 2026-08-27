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
        return _Response()

    monkeypatch.setattr(reload_with_notice.request, "urlopen", urlopen)
    assert reload_with_notice._notify_dashboard(SimpleNamespace(port=8080, ssl_certfile=None))
    assert seen == {
        "url": "http://127.0.0.1:8080/api/internal/restart-notice",
        "token": "shared-token",
        "timeout": 1,
        "context": None,
    }


def test_reload_waits_only_after_dashboard_accepts_notice(monkeypatch):
    calls = []
    supervisor = SimpleNamespace(config=SimpleNamespace(port=8080, ssl_certfile=None))
    monkeypatch.setattr(reload_with_notice, "_notify_dashboard", lambda _config: True)
    monkeypatch.setattr(reload_with_notice.time, "sleep", lambda seconds: calls.append(("sleep", seconds)))
    monkeypatch.setattr(reload_with_notice, "_original_restart", lambda _self: calls.append(("restart", None)))

    reload_with_notice._restart_with_notice(supervisor)

    assert calls == [("sleep", 3), ("restart", None)]
