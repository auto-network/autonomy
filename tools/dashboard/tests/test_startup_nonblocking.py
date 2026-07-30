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

import time

from starlette.testclient import TestClient

from tools.dashboard import link_serving_supervisor


def test_lifespan_startup_does_not_block_on_bootstrap(
    test_app, monkeypatch
):
    started = {"called": False, "finished": False}

    def slow_bootstrap(orgs=None):
        started["called"] = True
        time.sleep(3.0)  # stand in for real connector bring-up
        started["finished"] = True
        return None

    monkeypatch.setattr(link_serving_supervisor, "bootstrap", slow_bootstrap)
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)

    t0 = time.monotonic()
    with TestClient(test_app) as client:
        entered = time.monotonic() - t0
        # The lifespan completed (startup returned) far faster than the
        # 3s bootstrap could have — proving it is fire-and-forget, not
        # awaited. Generous ceiling so unrelated startup work never flakes.
        assert entered < 2.0, (
            f"startup blocked for {entered:.1f}s — bootstrap is being awaited")
        # And the app actually answers during the window bootstrap is still
        # running in the background.
        resp = client.get("/api/version")
        assert resp.status_code == 200
        assert started["called"] is True
        assert started["finished"] is False  # still running in the background
