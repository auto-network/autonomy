"""Red-first contract for the local Caddy Service gateway (auto-dbex5)."""

from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace

import pytest

from tools.dashboard import service_gateway
from tools.dashboard.service_publication import ServiceTargetDescriptor


HOSTNAME = "port-8000.persona-77827e972ba4c37d4215.serve.auto.network"
CONTAINER_ID = "ab" * 32


def _route(**changes):
    values = {
        "reservation_id": "91674161-2d14-55a0-be9d-21237d02c2dc",
        "hostname": HOSTNAME,
        "session_id": "auto-0831-011653",
        "container_id": CONTAINER_ID,
        "network": "autonomy_default",
        "port": 8000,
        "expires_at": "2026-08-31T05:20:43.000Z",
    }
    values.update(changes)
    return service_gateway.ServiceGatewayRoute(**values)


def test_rendered_caddyfile_has_one_exact_route_and_no_http3_or_tcp_admin():
    rendered = service_gateway.render_caddyfile([_route()])

    assert "admin unix//run/autonomy-service-gateway/admin.sock|0660" in rendered
    assert "persist_config off" in rendered
    assert "servers :9443" in rendered
    assert "protocols h1 h2" in rendered
    assert "strict_sni_host on" in rendered
    assert f"host {HOSTNAME}" in rendered
    assert "reverse_proxy auto-0831-011653:8000" in rendered
    assert "tls /run/autonomy-service-gateway-certs/tls.crt /run/autonomy-service-gateway-certs/tls.key" in rendered
    assert "abort" in rendered
    assert "h3" not in rendered
    assert ":2019" not in rendered
    assert "docker.sock" not in rendered


def test_unavailable_host_is_exact_and_never_falls_through_to_an_upstream():
    rendered = service_gateway.render_caddyfile([], unavailable_hosts=[HOSTNAME])

    assert f"host {HOSTNAME}" in rendered
    assert 'respond "Service unavailable" 503' in rendered
    assert "reverse_proxy" not in rendered
    assert "abort" in rendered


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("hostname", "*.serve.auto.network"),
        ("hostname", "app.example.com"),
        ("hostname", "app.persona.serve.auto.network\nreverse_proxy attacker:80"),
        ("session_id", "session:80"),
        ("session_id", "session name"),
        ("container_id", "not-hex"),
        ("network", "bridge"),
        ("port", 0),
        ("port", True),
    ],
)
def test_route_refuses_non_descriptor_values(field, value):
    with pytest.raises(ValueError):
        _route(**{field: value})


def test_route_is_derived_from_the_existing_live_target_descriptor(monkeypatch):
    reservation = SimpleNamespace(
        payload={
            "app_label": "port-8000",
            "persona_label": "persona-77827e972ba4c37d4215",
        }
    )
    descriptor = ServiceTargetDescriptor(
        session_id="auto-0831-011653",
        container_id=CONTAINER_ID,
        network="autonomy_default",
        port=8000,
        checked_at="2026-08-31T05:20:38.000Z",
        expires_at="2026-08-31T05:20:43.000Z",
    )
    monkeypatch.setattr(
        service_gateway.service_publication,
        "_reservation_for_target",
        lambda org, key, serving=False: reservation,
    )

    async def resolve(org, key):
        assert (org, key) == ("autonomy", "91674161-2d14-55a0-be9d-21237d02c2dc")
        return descriptor

    monkeypatch.setattr(
        service_gateway.service_publication, "resolve_service_target", resolve
    )

    route = asyncio.run(
        service_gateway.resolve_gateway_route(
            "autonomy", "91674161-2d14-55a0-be9d-21237d02c2dc"
        )
    )
    assert route == _route()


def test_admin_load_uses_only_the_unix_socket_and_posts_the_complete_config(monkeypatch):
    seen = {}

    class FakeResponse:
        status = 200
        reason = "OK"

        def read(self):
            return b""

    class FakeConnection:
        def __init__(self, socket_path, timeout):
            seen["init"] = (socket_path, timeout)

        def request(self, method, path, *, body, headers):
            seen["request"] = (method, path, body, headers)

        def getresponse(self):
            return FakeResponse()

        def close(self):
            seen["closed"] = True

    monkeypatch.setattr(service_gateway, "UnixHTTPConnection", FakeConnection)
    rendered = service_gateway.render_caddyfile([_route()])
    service_gateway.load_caddyfile(rendered, timeout=3.0)

    assert seen["init"] == (service_gateway.ADMIN_SOCKET, 3.0)
    assert seen["request"] == (
        "POST",
        "/load",
        rendered.encode(),
        {"Content-Type": "text/caddyfile", "Content-Length": str(len(rendered.encode()))},
    )
    assert seen["closed"] is True


def test_admin_load_surfaces_failed_config_without_retrying(monkeypatch):
    class FakeResponse:
        status = 400
        reason = "Bad Request"

        def read(self):
            return b"adapt failed"

    class FakeConnection:
        def __init__(self, socket_path, timeout):
            pass

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            return FakeResponse()

        def close(self):
            pass

    monkeypatch.setattr(service_gateway, "UnixHTTPConnection", FakeConnection)
    with pytest.raises(service_gateway.ServiceGatewayControlError, match="adapt failed"):
        service_gateway.load_caddyfile("bad config")
