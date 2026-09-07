"""The DNS process must refuse a relay address it cannot answer with, and an
answer fault must be logged rather than dropped silently.

2026-09-07: deploy.sh wrote DNS_RELAY_IP=registry.auto.network (a hostname);
every A answer raised inside the datagram handler and vanished under a
blanket suppress, while NS/SOA/TXT kept answering. The deploy proof (an A
query) failed and was read as a script quirk. Whole-zone outage.
"""

from __future__ import annotations

import asyncio
import logging
import struct

import pytest

from tools.network.registry import dns_server
from tools.network.registry.dns_lookup import _build_query


@pytest.mark.parametrize("value", ["registry.auto.network", "", "0.0.0.0", "::1", "5.161.219"])
def test_relay_ip_must_be_a_routable_ipv4(value):
    with pytest.raises(SystemExit, match="--relay-ip must be"):
        dns_server.validate_relay_ip(value)


def test_relay_ip_accepts_and_normalizes_a_dotted_ipv4():
    assert dns_server.validate_relay_ip(" 5.161.219.195 ") == "5.161.219.195"


def test_service_refuses_a_hostname_relay_ip_at_construction():
    with pytest.raises(SystemExit):
        dns_server.DnsService(relay_ip="registry.auto.network", node_id="t",
                              registry_url="http://127.0.0.1:1")


def test_service_answers_a_for_any_zone_name_with_the_relay_ip(monkeypatch):
    service = dns_server.DnsService(relay_ip="5.161.219.195", node_id="t",
                                    registry_url="http://127.0.0.1:1")

    async def no_refresh():
        return None
    monkeypatch.setattr(service._cache, "refresh_if_stale", no_refresh)
    monkeypatch.setattr(service.state, "zones", lambda: ["serve.auto.network", "autonomy.taplink.net"])
    for name in ("themes.autonomy.taplink.net", "probe.serve.auto.network"):
        query, _ = _build_query(name, 1, recursive=False)
        reply = asyncio.run(service.answer(query, "203.0.113.9", tcp=False))
        assert reply is not None and reply[3] & 0xF == 0
        assert struct.unpack(">H", reply[6:8])[0] == 1
        assert ".".join(str(b) for b in reply[-4:]) == "5.161.219.195"


def test_udp_answer_fault_is_logged_not_swallowed(caplog):
    class _Boom:
        async def answer(self, raw, source, *, tcp):
            raise ValueError("invalid literal for int() with base 10: 'registry'")

    protocol = dns_server._UdpProtocol(_Boom())
    dns_server._last_fault_log = 0.0
    before = dns_server.answer_faults
    with caplog.at_level(logging.ERROR, logger="registry.dns"):
        asyncio.run(protocol._respond(b"\x00" * 12, ("203.0.113.9", 5353)))
    assert dns_server.answer_faults == before + 1
    records = [r for r in caplog.records if "DNS answer failed" in r.getMessage()]
    assert len(records) == 1 and records[0].exc_info is not None
    assert "registry" in "".join(caplog.text.splitlines()[-3:]) or "ValueError" in caplog.text
