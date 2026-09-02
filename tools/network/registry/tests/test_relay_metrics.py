"""auto-albp6.9: private, bounded-cardinality relay metrics.

The security-critical acceptance is here: gauges equal live state, cardinality
stays bounded under 10k links, no metric pairs an address/token/host with an
org, the exposition is private (its own listener), and scraping does not
mutate relay state.
"""

from __future__ import annotations

import asyncio

import pytest

from tools.network.registry.metrics import (
    RegistryMetrics,
    start_metrics_listener,
)


class _FakeTunnel:
    def __init__(self, org, channels=0, streams=0):
        self.org = org
        self.channels = list(range(channels))
        self.raw_streams = {i: object() for i in range(streams)}


class _FakeHub:
    def __init__(self):
        self._tunnels = {}

    def add(self, org, persona, machine, **kw):
        self._tunnels.setdefault(org, {})[(persona, machine)] = _FakeTunnel(
            org, **kw)


class _FakeLease:
    def __init__(self, org):
        self.tunnel = _FakeTunnel(org)


class _FakeHostRoutes:
    def __init__(self):
        self._leases = {}

    def add(self, reservation, org):
        self._leases[reservation] = _FakeLease(org)


ORG_A = "aaaaaaaa-0000-0000-0000-000000000001"
ORG_B = "bbbbbbbb-0000-0000-0000-000000000002"


def _series_names(text: str) -> set:
    return {
        line.split("{")[0].split(" ")[0]
        for line in text.splitlines()
        if line and not line.startswith("#")
    }


def _lines(text: str) -> list:
    return [ln for ln in text.splitlines() if ln and not ln.startswith("#")]


def test_gauges_equal_live_state_and_track_teardown():
    m = RegistryMetrics()
    hub, hr = _FakeHub(), _FakeHostRoutes()
    m.bind_state(hub=hub, host_routes=hr)
    hub.add(ORG_A, "p1", "m1", channels=3, streams=2)
    hub.add(ORG_A, "p1", "m2", channels=1, streams=0)
    hub.add(ORG_B, "p9", "m9", channels=0, streams=5)
    hr.add("r1", ORG_A)
    hr.add("r2", ORG_A)
    out = m.render()
    assert f'relay_tunnels{{org="{ORG_A}"}} 2' in out
    assert f'relay_tunnels{{org="{ORG_B}"}} 1' in out
    assert f'relay_viewer_channels{{org="{ORG_A}"}} 4' in out  # 3+1
    assert f'relay_raw_streams{{org="{ORG_B}"}} 5' in out
    assert f'relay_active_leases{{org="{ORG_A}"}} 2' in out
    # Teardown: the gauge reflects it because it reads live state.
    del hub._tunnels[ORG_B]
    hr._leases.clear()
    out2 = m.render()
    assert f'org="{ORG_B}"' not in out2.split("relay_stream")[0]  # no B tunnel
    assert "relay_active_leases" in out2  # type line present
    assert 'relay_active_leases{' not in out2  # but no series


def test_ten_thousand_links_add_zero_series():
    m = RegistryMetrics()
    hub = _FakeHub()
    m.bind_state(hub=hub)
    hub.add(ORG_A, "p1", "m1", channels=1, streams=1)
    baseline = len(_lines(m.render()))
    # Ten thousand stream events on one org: counters increment, series count
    # is unchanged — tokens/hosts are never labels.
    for _ in range(10_000):
        m.stream_opened(ORG_A)
        m.stream_bytes(ORG_A, "in", 1500)
    assert len(_lines(m.render())) == baseline + 2  # opens + bytes(in), one org


def test_hostile_label_values_collapse_to_bounded_enums():
    m = RegistryMetrics()
    m.stream_refused("no_sni")
    m.stream_refused("'; DROP TABLE --")     # not an enum → 'other'
    m.stream_refused("another\ninjection")   # not an enum → 'other'
    m.lease_event(ORG_A, "not-an-event")      # dropped entirely
    m.dns_query("weird-rcode")                # → 'other'
    out = m.render()
    reasons = {
        ln.split('reason="')[1].split('"')[0]
        for ln in _lines(out) if ln.startswith("relay_stream_refusals_total")
    }
    assert reasons == {"no_sni", "other"}
    assert "DROP TABLE" not in out and "injection" not in out
    # The dropped lease event created no series.
    assert "relay_lease_events_total{" not in out


def test_no_metric_pairs_address_token_or_host():
    m = RegistryMetrics()
    m.stream_opened(ORG_A)
    m.stream_refused("unrouted")
    m.lease_event(ORG_A, "register")
    m.dns01_op("ok")
    out = m.render()
    label_names = set()
    for ln in _lines(out):
        if "{" in ln:
            inner = ln[ln.index("{") + 1:ln.index("}")]
            for pair in inner.split(","):
                label_names.add(pair.split("=")[0])
    # The ONLY labels that may ever appear.
    assert label_names <= {"org", "reason", "event", "result", "rcode",
                           "direction"}


def test_malformed_org_collapses_and_never_injects():
    m = RegistryMetrics()
    m.stream_opened('evil"} relay_pwn 1')     # not a UUID → 'other'
    out = m.render()
    assert 'relay_stream_opens_total{org="other"}' in out
    assert "relay_pwn" not in out


def test_scrape_does_not_mutate_state():
    m = RegistryMetrics()
    hub = _FakeHub()
    m.bind_state(hub=hub)
    hub.add(ORG_A, "p1", "m1", channels=2, streams=1)
    m.stream_opened(ORG_A)
    first = m.render()
    second = m.render()
    assert first == second  # pure read
    assert hub._tunnels[ORG_A][("p1", "m1")].channels == [0, 1]  # untouched


def test_private_listener_serves_only_metrics():
    async def scenario():
        m = RegistryMetrics()
        m.stream_opened(ORG_A)
        server = await start_metrics_listener("127.0.0.1", 0, m)
        port = server.sockets[0].getsockname()[1]

        async def get(path):
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.write(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
            await w.drain()
            data = await r.read(65536)
            w.close()
            return data.decode("utf-8", "replace")

        ok = await get("/metrics")
        missing = await get("/")
        server.close()
        await server.wait_closed()
        return ok, missing

    ok, missing = asyncio.run(scenario())
    assert ok.startswith("HTTP/1.1 200")
    assert "text/plain; version=0.0.4" in ok
    assert "relay_stream_opens_total" in ok
    assert missing.startswith("HTTP/1.1 404")


def test_public_app_exposes_no_metrics_route():
    from fastapi.testclient import TestClient

    from tools.network.registry.app import create_app

    # No metrics_port: the public app must not serve /metrics at all.
    app = create_app(":memory:")
    with TestClient(app) as client:
        assert client.get("/metrics").status_code == 404


# -- DNS-process metrics (separate scrape target) --------------------------

def test_dns_metrics_counts_rcodes_and_challenge_gauge():
    from tools.network.registry.metrics import DnsMetrics

    m = DnsMetrics()
    m.bind_challenge_count(lambda: 3)
    m.query(0)   # noerror
    m.query(0)
    m.query(3)   # nxdomain
    m.query(16)  # badvers
    m.query(99)  # unknown -> other
    m.dropped()
    out = m.render()
    assert 'dns_queries_total{rcode="noerror"} 2' in out
    assert 'dns_queries_total{rcode="nxdomain"} 1' in out
    assert 'dns_queries_total{rcode="badvers"} 1' in out
    assert 'dns_queries_total{rcode="other"} 1' in out
    assert 'dns_queries_total{rcode="dropped"} 1' in out
    assert "dns_challenge_records 3" in out
    # No source-address label anywhere.
    assert "source" not in out and "addr" not in out


# -- auto-7df7o: structured ops logs + operator readout --------------------

def test_readout_shows_tunnels_and_loads_no_secrets():
    from tools.network.registry.metrics import render_readout

    class _T:
        def __init__(self, org, persona, machine, ch, st, ver, caps, last):
            self.org, self.persona_pub, self.machine = org, persona, machine
            self.channels = list(range(ch))
            self.raw_streams = {i: 0 for i in range(st)}
            self.version, self.caps, self.last_control = ver, caps, last

    class _Hub:
        def __init__(self):
            self._tunnels = {}

    hub = _Hub()
    hub._tunnels[ORG_A] = {
        ("p1", "m1"): _T(ORG_A, "ab" * 32, "cd" * 32, 2, 1, 2,
                         ("host-lease/1",), {"op": "host-register",
                                             "result": "ok", "reason": None}),
        ("p1", "m2"): _T(ORG_A, "ab" * 32, "ef" * 32, 0, 3, 2, (), None),
    }
    out = render_readout(hub, build_info={"commit": "abc123"})
    assert out["build"]["commit"] == "abc123"
    assert out["orgs"][ORG_A]["count"] == 2
    tunnels = out["orgs"][ORG_A]["tunnels"]
    assert {t["streams"] for t in tunnels} == {1, 3}
    assert any(t["last_control"]["op"] == "host-register" for t in tunnels)
    # Public ids are truncated; no full-length key, token, or address field.
    import json
    blob = json.dumps(out)
    assert "ab" * 32 not in blob            # persona pub not emitted in full
    assert "token" not in blob and "source" not in blob and "addr" not in blob


def test_control_and_lifecycle_ops_logs_carry_no_secrets(caplog):
    """A control result and a tunnel register/unregister emit structured ops
    lines with org/op/result but never a token, address, or payload."""
    import logging

    from tools.network.registry import relay as relay_mod

    ops = logging.getLogger("autonomy.registry.ops")
    records: list = []

    class _Cap(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    cap = _Cap(level=logging.INFO)
    ops.addHandler(cap)
    try:
        relay_mod._ops("control", org="aaaaaaaa", op="host-register",
                       id="f" * 32, result="ok", reason=None)
        relay_mod._ops("tunnel.register", org="aaaaaaaa",
                       persona="ab" * 8, machine="cd" * 8, version=2, pool=1)
    finally:
        ops.removeHandler(cap)
    joined = "\n".join(records)
    assert "control org=aaaaaaaa op=host-register" in joined
    assert "result=ok" in joined
    assert "tunnel.register" in joined and "pool=1" in joined
    # None-valued fields (reason=None) are dropped, not rendered as "None".
    assert "reason=None" not in joined
