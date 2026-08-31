"""Startup must not block on serving bring-up.

The serving supervisor's ``bootstrap()`` reconciles serving per org, which
now does real tunnel-connector bring-up (network work) and can take tens of
seconds once grants are live. It must run as a background task so the
lifespan returns immediately — otherwise the server accepts connections but
answers nothing until bootstrap finishes (the ~55s restart dead window,
2026-07-30). This drives the real lifespan with a deliberately slow
bootstrap and asserts startup returns well before it could have completed.
"""

from __future__ import annotations

import threading
import time

from starlette.testclient import TestClient

from tools.dashboard import link_serving_supervisor, web_gateway_supervisor


def test_lifespan_startup_does_not_block_on_bootstrap(
    test_app, monkeypatch
):
    started = {"called": False, "finished": False}
    gateway_worker = {"started": False, "stopped": False}
    # Gate the fake bootstrap on an event rather than a wall-clock sleep: the
    # proof that startup is fire-and-forget is that the lifespan returns while
    # bootstrap is still blocked, which holds regardless of scheduling latency.
    # A wall-clock threshold (entered < 2s vs a 3s sleep) flakes under -n 8
    # CPU contention even though the code is correct.
    release = threading.Event()

    def slow_bootstrap(orgs=None):
        started["called"] = True
        release.wait(timeout=30)  # blocks until the test releases it
        started["finished"] = True
        return None

    monkeypatch.setattr(link_serving_supervisor, "bootstrap", slow_bootstrap)
    async def start_gateway_worker(_event_bus):
        gateway_worker["started"] = True

    async def stop_gateway_worker():
        gateway_worker["stopped"] = True

    monkeypatch.setattr(web_gateway_supervisor, "start_worker", start_gateway_worker)
    monkeypatch.setattr(web_gateway_supervisor, "stop_worker", stop_gateway_worker)
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)

    try:
        with TestClient(test_app) as client:
            # Startup returned even though bootstrap is still blocked on the
            # event — that is the fire-and-forget proof, with no timing margin.
            resp = client.get("/api/version")
            assert resp.status_code == 200
            assert gateway_worker["started"] is True
            # The background task was kicked off (poll briefly; it runs on
            # another thread and may not have been scheduled yet).
            for _ in range(200):
                if started["called"]:
                    break
                time.sleep(0.02)
            assert started["called"] is True
            # Still running: blocked on the event, not finished — no race with
            # a wall-clock sleep.
            assert started["finished"] is False
            # Release before leaving the context so lifespan shutdown does not
            # wait on the background task (keeps the test fast).
            release.set()
    finally:
        release.set()  # safety if an assertion above raised first
    assert gateway_worker["stopped"] is True
