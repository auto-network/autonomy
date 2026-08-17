"""The public server must never retain bearer-bearing request paths."""

from __future__ import annotations

import asyncio
import logging
import sys

from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from tools.network.registry import __main__ as registry_main


def test_entrypoint_disables_http_and_websocket_route_logging(monkeypatch):
    app = object()
    app_calls: list[tuple[str, str, dict]] = []
    run_calls: list[tuple[object, dict[str, object]]] = []

    def fake_create_app(db: str, *, base_url: str, build_info: dict, turn_issuer=None):
        assert turn_issuer is None
        app_calls.append((db, base_url, build_info))
        return app

    monkeypatch.setattr(registry_main, "create_app", fake_create_app)
    monkeypatch.setattr(
        registry_main.uvicorn,
        "run",
        lambda passed_app, **kwargs: run_calls.append((passed_app, kwargs)),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "python -m tools.network.registry",
            "--db",
            "/tmp/registry-test.db",
            "--host",
            "127.0.0.2",
            "--port",
            "18477",
            "--base-url",
            "https://relay.test",
            "--version-file",
            "/missing/revision.json",
        ],
    )

    registry_main.main()

    assert app_calls == [(
        "/tmp/registry-test.db",
        "https://relay.test",
        {"commit": "unknown", "dirty": None, "built_at": None},
    )]
    assert run_calls == [
        (
            app,
            {
                "host": "127.0.0.2",
                "port": 18477,
                # HTTP request paths are emitted by uvicorn.access.
                "access_log": False,
                # WebSocket paths are emitted by uvicorn.error at INFO even
                # when the HTTP access logger is disabled.
                "log_level": "warning",
                # Only Caddy's loopback hop supplies authoritative client IPs.
                "proxy_headers": True,
                "forwarded_allow_ips": registry_main.FORWARDED_ALLOW_IPS,
            },
        )
    ]

    # access_log=False only disables uvicorn.access.  Uvicorn records
    # WebSocket paths through uvicorn.error at INFO, so the actual safety
    # property is that INFO cannot be emitted by that logger.  Exercise the
    # captured entrypoint configuration through Uvicorn itself, while
    # restoring its process-global logging configuration for the test suite.
    logger_names = ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi")
    logger_state = {
        name: (
            logging.getLogger(name).level,
            list(logging.getLogger(name).handlers),
            logging.getLogger(name).propagate,
            logging.getLogger(name).disabled,
        )
        for name in logger_names
    }
    try:
        kwargs = run_calls[0][1]
        registry_main.uvicorn.Config(
            app,
            access_log=kwargs["access_log"],
            log_level=kwargs["log_level"],
        )
        assert logging.getLogger("uvicorn.error").getEffectiveLevel() > logging.INFO
    finally:
        for name, (level, handlers, propagate, disabled) in logger_state.items():
            logger = logging.getLogger(name)
            logger.setLevel(level)
            logger.handlers = handlers
            logger.propagate = propagate
            logger.disabled = disabled


def test_only_loopback_proxy_can_supply_the_limiter_source_address():
    async def probe(peer: str, forwarded: str) -> tuple[str, int]:
        seen: list[tuple[str, int]] = []

        async def app(scope, receive, send):
            seen.append(scope["client"])

        middleware = ProxyHeadersMiddleware(
            app, trusted_hosts=registry_main.FORWARDED_ALLOW_IPS
        )
        scope = {
            "type": "http",
            "scheme": "http",
            "client": (peer, 12345),
            "headers": [(b"x-forwarded-for", forwarded.encode("ascii"))],
        }
        await middleware(scope, None, None)
        return seen[0]

    # A direct remote caller cannot choose its source key.
    assert asyncio.run(probe("203.0.113.5", "10.0.0.1")) == (
        "203.0.113.5",
        12345,
    )
    # Caddy's loopback hop is trusted. In a multi-value header Uvicorn walks
    # from the trusted right edge, so a client-added left value is not used.
    assert asyncio.run(
        probe("127.0.0.1", "10.0.0.1, 198.51.100.9")
    ) == ("198.51.100.9", 0)
