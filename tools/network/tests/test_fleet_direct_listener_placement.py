"""Where the direct listener lives, and who pulls.

Default: the connector subprocess binds the listener (it already builds and
streams checkpoints for relay serves and carries no operator UI); the
dashboard binds only loopback-ephemeral. A machine may also opt out of
pulling over direct while still announcing and serving.
"""

from __future__ import annotations

import pytest

from tools.network import fleet_direct_config as fdc

pytestmark = pytest.mark.asyncio


def test_listener_bind_follows_serve_in():
    cfg = fdc.FleetDirectConfig("0.0.0.0", 9410, ("ws://100.1.2.3:9410",))
    assert cfg.serve_in == "connector"
    assert fdc.listener_bind(cfg, "connector") == ("0.0.0.0", 9410)
    assert fdc.listener_bind(cfg, "dashboard") == ("127.0.0.1", 0)
    cfg = fdc.FleetDirectConfig("0.0.0.0", 9410, (), serve_in="dashboard")
    assert fdc.listener_bind(cfg, "dashboard") == ("0.0.0.0", 9410)
    assert fdc.listener_bind(cfg, "connector") == ("127.0.0.1", 0)
    disabled = fdc.FleetDirectConfig("127.0.0.1", 0, ())
    assert fdc.listener_bind(disabled, "connector") == ("127.0.0.1", 0)


async def test_connector_binds_rebinds_and_stops_its_listener(monkeypatch):
    """ensure_direct_listener() is idempotent, tracks the armed scheduler's
    bind, retires a replaced scheduler's listener, and stops when disabled."""
    from types import SimpleNamespace

    from tools.network import fleet_relay_sync as frs

    class FakeServer:
        def __init__(self, host, port):
            self._host, self._port, self.running = host, port, False
            self.stops = 0

        @property
        def host(self):
            return self._host

        @property
        def port(self):
            return self._port

        async def start(self):
            if self._port == 1:
                raise OSError("address in use")
            self.running = True
            self._port = self._port or 54321
            return self._port

        async def stop(self):
            self.running = False
            self.stops += 1

    def scheduler(host, port):
        return SimpleNamespace(
            config=SimpleNamespace(listen_host=host, listen_port=port),
            server=FakeServer(host, port),
        )

    rt = frs.ConnectorFleetRuntime()
    assert await rt.ensure_direct_listener() is None          # unarmed

    rt.scheduler = scheduler("0.0.0.0", 9410)
    assert await rt.ensure_direct_listener() == ("0.0.0.0", 9410)
    assert await rt.ensure_direct_listener() == ("0.0.0.0", 9410)  # idempotent
    first = rt.scheduler.server

    # re-arm with a different scheduler: the old listener is retired first
    rt._retired_listeners.append(first)
    rt.scheduler = scheduler("0.0.0.0", 9411)
    assert await rt.ensure_direct_listener() == ("0.0.0.0", 9411)
    assert first.stops == 1 and not first.running

    # bind failure is reported, not raised
    rt.scheduler = scheduler("0.0.0.0", 1)
    assert await rt.ensure_direct_listener() is None
    assert rt.direct_listener is None

    # disabled (port 0): a running listener is stopped
    live = scheduler("0.0.0.0", 9412)
    rt.scheduler = live
    assert await rt.ensure_direct_listener() == ("0.0.0.0", 9412)
    live.config.listen_port = 0
    assert await rt.ensure_direct_listener() is None
    assert live.server.stops == 1


async def test_real_listener_binds_in_the_connector_runtime_on_loopback():
    """A real FleetDirectServer through ensure_direct_listener(): binds an
    ephemeral loopback port, reports it, stops cleanly."""
    from types import SimpleNamespace

    from tools.network import fleet_relay_sync as frs
    from tools.network.fleet_sync_channel import FleetAuthenticator, FleetDirectServer
    from tools.network.idkit import KeyPair

    root = KeyPair.generate()
    auth = FleetAuthenticator(
        KeyPair.generate(), root_pub=root.public_hex, roster_entries=lambda: (),
    )

    async def handler(*_a, **_k):
        return None

    server = FleetDirectServer(auth, handler, host="127.0.0.1", port=0)
    rt = frs.ConnectorFleetRuntime()
    # port 0 means "disabled" to ensure_direct_listener; drive start directly
    # to prove the running/host/port surface the loop relies on.
    assert server.running is False
    port = await server.start()
    assert server.running is True and server.port == port and server.host == "127.0.0.1"
    rt.scheduler = SimpleNamespace(
        config=SimpleNamespace(listen_host="127.0.0.1", listen_port=port), server=server,
    )
    assert await rt.ensure_direct_listener() == ("127.0.0.1", port)
    rt.scheduler.config.listen_port = 0
    assert await rt.ensure_direct_listener() is None
    assert server.running is False
