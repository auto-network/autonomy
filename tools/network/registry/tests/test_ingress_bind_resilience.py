"""The serve edge is a feature, not a precondition (outage 2026-09-03).

An unattended upgrade recycled netplan without the serve floating IP, the
address vanished, and the raw-stream ingress could not bind it. The bind error
raised out of the FastAPI startup hook, so the whole registry process died and
systemd restart-looped — taking the registry API and the relay down with the
serve edge, neither of which uses that address at all.

The ingress now degrades: a bind failure is logged on the ops sink, the process
keeps serving everything else, and a background task rebinds when the address
returns.
"""

from __future__ import annotations

import logging

from fastapi.testclient import TestClient

from tools.network.registry.app import create_app


#: An address this process cannot possibly bind — the stand-in for a serve
#: floating IP that is not configured on any interface.
UNBINDABLE_HOST = "192.0.2.1"  # TEST-NET-1, guaranteed not local


def test_absent_serve_address_degrades_instead_of_killing_the_registry(clock):
    """The registry API must answer even when the serve edge cannot bind."""
    ops = logging.getLogger("autonomy.registry.ops")
    records: list = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    capture = _Capture(level=logging.INFO)
    ops.addHandler(capture)
    try:
        app = create_app(
            ":memory:", now_fn=clock, secure_cookies=False,
            stream_ingress_host=UNBINDABLE_HOST, stream_ingress_port=443,
        )
        # Entering the TestClient runs the startup hooks. Before the fix this
        # raised OSError here and the process died.
        with TestClient(app) as client:
            assert client.get("/healthz").status_code == 200
            # Degraded, and honest about it: no listener, retry scheduled.
            assert app.state.stream_ingress is None
            assert app.state.stream_ingress_rebind is not None
    finally:
        ops.removeHandler(capture)

    # The operator can see WHY the edge is down, on a sink that survives the
    # production WARNING root level.
    assert any("stream.ingress.bind-failed" in m for m in records)


def test_bindable_serve_address_still_serves_the_edge(clock):
    """The happy path is unchanged: a bindable address listens, no retry."""
    app = create_app(
        ":memory:", now_fn=clock, secure_cookies=False,
        stream_ingress_host="127.0.0.1", stream_ingress_port=0,
    )
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert app.state.stream_ingress is not None
        assert app.state.stream_ingress_rebind is None
