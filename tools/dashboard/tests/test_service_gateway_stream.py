"""Red-first contract for RelayKit TLS streams entering local Caddy."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from tools.dashboard import link_serving, service_gateway_stream
from tools.dashboard.service_publication import ServicePublicationError


RESERVATION = "91674161-2d14-55a0-be9d-21237d02c2dc"
HOST = "port-8000.persona-77827e972ba4c37d4215.serve.auto.network"


def _reservation(state="active"):
    return SimpleNamespace(
        payload={
            "app_label": "port-8000",
            "persona_label": "persona-77827e972ba4c37d4215",
            "state": state,
        }
    )


def test_active_reservation_dials_only_the_fixed_local_caddy(monkeypatch):
    seen = {}

    def reservation_for_target(org, key, *, serving=False):
        seen["reservation"] = (org, key, serving)
        return _reservation()

    async def open_connection(host, port):
        seen["dial"] = (host, port)
        return "reader", "writer"

    monkeypatch.setattr(
        service_gateway_stream.service_publication,
        "_reservation_for_target",
        reservation_for_target,
    )
    monkeypatch.setattr(asyncio, "open_connection", open_connection)

    handler = service_gateway_stream.LocalCaddyStreamHandler("autonomy")
    result = asyncio.run(handler(HOST, RESERVATION))

    assert result == ("reader", "writer")
    assert seen == {
        "reservation": ("autonomy", RESERVATION, False),
        "dial": ("service-gateway", 9443),
    }


def test_paused_reservation_still_dials_caddy_for_its_paused_response(monkeypatch):
    monkeypatch.setattr(
        service_gateway_stream.service_publication,
        "_reservation_for_target",
        lambda *_args, **_kwargs: _reservation("paused"),
    )

    async def open_connection(_host, _port):
        return "reader", "writer"

    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    handler = service_gateway_stream.LocalCaddyStreamHandler("autonomy")

    assert asyncio.run(handler(HOST, RESERVATION)) == ("reader", "writer")


def test_unknown_released_or_wrong_host_refuses_without_dial(monkeypatch):
    dialed = False

    async def open_connection(_host, _port):
        nonlocal dialed
        dialed = True

    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    handler = service_gateway_stream.LocalCaddyStreamHandler("autonomy")

    monkeypatch.setattr(
        service_gateway_stream.service_publication,
        "_reservation_for_target",
        lambda *_args, **_kwargs: _reservation(),
    )
    assert asyncio.run(handler("other." + HOST, RESERVATION)) is None

    def refused(*_args, **_kwargs):
        raise ServicePublicationError("reservation_released", 409)

    monkeypatch.setattr(
        service_gateway_stream.service_publication, "_reservation_for_target", refused
    )
    assert asyncio.run(handler(HOST, RESERVATION)) is None
    assert dialed is False


def test_caddy_dial_failure_refuses_stream(monkeypatch):
    monkeypatch.setattr(
        service_gateway_stream.service_publication,
        "_reservation_for_target",
        lambda *_args, **_kwargs: _reservation(),
    )

    async def open_connection(_host, _port):
        raise ConnectionRefusedError

    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    handler = service_gateway_stream.LocalCaddyStreamHandler("autonomy")

    assert asyncio.run(handler(HOST, RESERVATION)) is None


def test_production_connector_negotiates_stream_cap_and_installs_handler():
    captured = {}

    def factory(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return object()

    link_serving._make_ice_serving_connector(
        "wss://relay.auto.network",
        "org-id",
        key=object(),
        cert=object(),
        channel_cert=object(),
        graph_org="autonomy",
        publisher=object(),
        min_backoff=0.2,
        max_backoff=5.0,
        machine_key=object(),
        connector_factory=factory,
    )

    assert "tls-stream/1" in captured["kwargs"]["caps"]
    assert "host-lease/1" in captured["kwargs"]["caps"]
    assert isinstance(
        captured["kwargs"]["stream_handler"],
        service_gateway_stream.LocalCaddyStreamHandler,
    )


def test_production_connector_without_warm_machine_key_stays_legacy():
    captured = {}

    def factory(*args, **kwargs):
        captured.update(kwargs)
        return object()

    link_serving._make_ice_serving_connector(
        "wss://relay.auto.network",
        "org-id",
        key=object(),
        cert=object(),
        channel_cert=object(),
        graph_org="autonomy",
        publisher=object(),
        min_backoff=0.2,
        max_backoff=5.0,
        machine_key=None,
        connector_factory=factory,
    )

    assert "caps" not in captured
    assert "stream_handler" not in captured
