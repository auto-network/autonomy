"""Session-card restart API and lifecycle admission tests."""


def test_live_session_queues_restart_with_settle_window(test_client, monkeypatch):
    from tools.dashboard import server

    queued = []
    monkeypatch.setattr(
        server._SESSION_LIFECYCLE_WORKER,
        "try_enqueue",
        lambda job: queued.append(job) or True,
    )

    resp = test_client.post("/api/session/auto-test-designer/restart")

    assert resp.status_code == 202
    assert resp.json()["status"] == "restarting"
    assert len(queued) == 1
    assert queued[0].action == "restart"
    assert queued[0].tmux_name == "auto-test-designer"
    assert queued[0].config["settle_seconds"] == 5.0
    assert queued[0].config["resume"] is True


def test_full_queue_does_not_enqueue_a_stop_half(test_client, monkeypatch):
    from tools.dashboard import server

    attempted = []

    def reject(job):
        attempted.append(job)
        return False

    monkeypatch.setattr(server._SESSION_LIFECYCLE_WORKER, "try_enqueue", reject)

    resp = test_client.post("/api/session/auto-test-designer/restart")

    assert resp.status_code == 503
    assert len(attempted) == 1
    assert attempted[0].action == "restart"
