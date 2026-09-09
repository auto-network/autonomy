"""Bounded, privacy-preserving evidence for connector failures."""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

from tools.network.idkit import KeyPair
from tools.network.relaykit import connector as connector_module
from tools.network.relaykit.connector import TunnelConnector
from tools.network.relaykit.frames import decode_frame


def _connector() -> TunnelConnector:
    return TunnelConnector(
        "ws://relay.invalid", "test-org", object(), object(),
        min_backoff=0.2, max_backoff=0.8,
                machine_key=KeyPair.generate(),
            )


class _Closed(ConnectionError):
    code = 4406

    def __init__(self, message, reason):
        super().__init__(message)
        self.reason = reason


def test_lifecycle_flood_is_one_immediate_line_plus_one_aggregate(
    monkeypatch, caplog,
):
    connector = _connector()
    now = [0.0]
    monkeypatch.setattr(connector_module.time, "monotonic", lambda: now[0])
    caplog.set_level(logging.WARNING, logger=connector_module.__name__)

    connector._log_disconnect(ConnectionError("first"), None, 0.2)
    for index in range(99):
        connector._log_disconnect(
            ConnectionError(f"suppressed-{index}"), 0.01, 0.8)
    assert len(caplog.records) == 1

    now[0] = 60.0
    connector._log_disconnect(_Closed("latest", "registry restart"), 0.25, 1.0)
    assert len(caplog.records) == 2
    aggregate = caplog.records[-1].getMessage()
    assert "suppressed=100" in aggregate
    assert "latest_lived=0.250s" in aggregate
    assert "latest_close_code=4406" in aggregate
    assert "latest_retry_delay=1.000s" in aggregate
    assert "_Closed:latest" in aggregate


def test_stable_service_reset_makes_next_exit_immediately_visible(
    monkeypatch, caplog,
):
    connector = _connector()
    monkeypatch.setattr(connector_module.time, "monotonic", lambda: 1.0)
    caplog.set_level(logging.WARNING, logger=connector_module.__name__)
    connector._log_disconnect(ConnectionError("first"), None, 0.2)
    connector._log_disconnect(ConnectionError("hidden"), None, 0.4)
    connector._reset_failure_log_suppression()
    connector._log_disconnect(None, connector._max_backoff, None)
    assert len(caplog.records) == 2
    assert "clean-exit" in caplog.records[-1].getMessage()
    assert "retry_delay=none" in caplog.records[-1].getMessage()


def test_lifecycle_log_redacts_url_address_ids_and_key_material(
    monkeypatch, caplog,
):
    connector = _connector()
    monkeypatch.setattr(connector_module.time, "monotonic", lambda: 1.0)
    caplog.set_level(logging.WARNING, logger=connector_module.__name__)
    token = "de" * 16
    key = "ab" * 32
    participant = "12345678-1234-4234-8234-123456789abc"
    address = "203.0.113.9"
    address_v6 = "2001:db8:1234:5678::42"
    opaque_credential = "ZxYwVuTsRqPoNmLkJiHgFeDcBa987654"
    url = f"wss://relay.invalid/v1/links/{token}/channel"
    secret = (
        f"{url} {address} {address_v6} {participant} {key} "
        f"{opaque_credential}"
    )
    connector._log_disconnect(_Closed(secret, secret), None, 0.2)
    line = caplog.records[-1].getMessage()
    for value in (
        token, key, participant, address, address_v6, opaque_credential, url,
    ):
        assert value not in line


def test_control_failures_log_only_correlation_operation_and_kind(caplog):
    connector = _connector()
    caplog.set_level(logging.WARNING, logger=connector_module.__name__)
    token = "cd" * 16

    async def run():
        with pytest.raises(ConnectionError):
            await connector.control("create-link", {"token": token})

        async def reject(_kind, _channel, payload):
            request = json.loads(decode_frame(
                connector_module.encode_frame(_kind, _channel, payload)
            ).payload)
            connector._resolve_ctrl_reply(json.dumps({
                "id": request["id"], "ok": False, "error": token,
            }).encode())

        connector._ctrl_send = reject
        reply = await connector.control("revoke-link", {"token": token})
        assert reply["ok"] is False

    asyncio.run(run())
    lines = [record.getMessage() for record in caplog.records]
    assert any("kind=no-live-tunnel" in line for line in lines)
    assert any("kind=rejected" in line for line in lines)
    assert all(token not in line for line in lines)
    assert all("args" not in line and "reply" not in line for line in lines)


def test_control_timeout_and_send_exception_are_bounded_and_secret_free(caplog):
    connector = _connector()
    caplog.set_level(logging.WARNING, logger=connector_module.__name__)
    secret = "ef" * 16

    async def run():
        async def no_reply(_kind, _channel, _payload):
            return None

        connector._ctrl_send = no_reply
        with pytest.raises(ConnectionError):
            await connector.control("create-link", {"token": secret}, timeout=0)

        async def explode(_kind, _channel, _payload):
            raise RuntimeError(secret)

        connector._ctrl_send = explode
        with pytest.raises(RuntimeError):
            await connector.control("revoke-link", {"token": secret})

    asyncio.run(run())
    lines = [record.getMessage() for record in caplog.records]
    assert any("kind=timeout" in line for line in lines)
    assert any("kind=exception" in line and "err=RuntimeError" in line
               for line in lines)
    assert all(secret not in line for line in lines)
