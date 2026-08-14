"""Fixed-memory public-relay abuse policy (graph://91e5e75f-7eb)."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import asyncio

import pytest
from fastapi.testclient import TestClient

import tools.network.registry.relay as relay
from tools.network.registry.abuse import (
    ACTIVE_LIMITS,
    ADMISSION_LIMITS,
    BYTE_LIMITS,
    AdmissionLimit,
    ByteLimit,
    RelayAbuseLimiter,
    ResolvedTicket,
    _CountMinRing,
    _ExactRing,
    _source_inputs,
)
from tools.network.registry.app import create_app
from tools.network.registry.relay import CLOSE_UNKNOWN_LINK, Tunnel, viewer_endpoint
from tools.network.registry.store import LinkGrant


class _Clock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _limiter(
    clock: _Clock,
    *,
    admission: dict[str, AdmissionLimit] | None = None,
    active: dict[str, int] | None = None,
    byte: dict[str, ByteLimit] | None = None,
    width: int = 8_192,
) -> RelayAbuseLimiter:
    admission_limits = dict(ADMISSION_LIMITS)
    admission_limits.update(admission or {})
    active_limits = dict(ACTIVE_LIMITS)
    active_limits.update(active or {})
    byte_limits = dict(BYTE_LIMITS)
    byte_limits.update(byte or {})
    return RelayAbuseLimiter(
        clock=clock,
        secret=b"s" * 32,
        admission_limits=admission_limits,
        active_limits=active_limits,
        byte_limits=byte_limits,
        width=width,
    )


def _resolved(limiter: RelayAbuseLimiter, suffix: str = ""):
    pre = limiter.begin(f"192.0.2.{10 + len(suffix)}")
    assert pre is not None
    resolved = limiter.resolve(pre, f"token-{suffix}", f"org-{suffix}")
    assert resolved is not None
    return resolved


def test_packed_count_min_geometry_is_the_frozen_fixed_allocation():
    limiter = RelayAbuseLimiter(secret=b"s" * 32)
    assert limiter.admission_allocated_bytes == 6_820_640


def test_source_keys_normalize_ipv4_mapped_and_ipv6_privacy_addresses():
    assert _source_inputs("192.0.2.9") == _source_inputs("::ffff:192.0.2.9")

    exact_a, network_a = _source_inputs("2001:db8:1234:5678::1")
    exact_b, network_b = _source_inputs("2001:db8:1234:5678:ffff::2")
    exact_c, network_c = _source_inputs("2001:db8:1234:9999::1")
    assert exact_a == exact_b  # exact IPv6 source is deliberately one /64
    assert exact_a != exact_c
    assert network_a == network_b == network_c  # source network is one /48


def test_exact_process_gate_charges_rejects_and_short_circuits_before_hashing():
    clock = _Clock()
    limiter = _limiter(
        clock, admission={"process": AdmissionLimit(1, 10)}
    )
    calls: list[str] = []
    original = limiter._digest

    def counted(scope: str, value: bytes) -> bytes:
        calls.append(scope)
        return original(scope, value)

    limiter._digest = counted  # type: ignore[method-assign]
    assert limiter.begin("192.0.2.1") is not None
    calls.clear()
    assert limiter.begin("192.0.2.2") is None
    assert calls == []
    assert limiter.snapshot()["decisions"]["process:burst"] == 1


def test_later_refusal_charges_prior_scope_but_does_not_hash_later_scope():
    clock = _Clock()
    limiter = _limiter(
        clock, admission={"source": AdmissionLimit(1, 10)}
    )
    assert limiter.begin("192.0.2.1") is not None
    calls: list[str] = []
    original = limiter._digest

    def counted(scope: str, value: bytes) -> bytes:
        calls.append(scope)
        return original(scope, value)

    limiter._digest = counted  # type: ignore[method-assign]
    assert limiter.begin("192.0.2.1") is None
    assert calls == ["source"]


def test_count_min_collision_can_only_conservatively_refuse():
    ring = _CountMinRing(width=1)
    limit = AdmissionLimit(1, 10)
    assert ring.check_and_charge(b"a" * 32, 0.0, limit) is None
    assert ring.check_and_charge(b"b" * 32, 0.0, limit) == "burst"


def test_slice_window_contains_every_event_in_preceding_ten_seconds():
    limit = AdmissionLimit(1, 10)
    within = _ExactRing()
    assert within.check_and_charge(4.999, limit) is None
    assert within.check_and_charge(14.999, limit) == "burst"

    outside = _ExactRing()
    assert outside.check_and_charge(4.999, limit) is None
    assert outside.check_and_charge(15.0, limit) is None


def test_active_counts_delete_on_last_close_and_release_is_idempotent():
    clock = _Clock()
    limiter = _limiter(clock)
    lease = limiter.acquire(_resolved(limiter))
    assert lease is not None
    assert limiter.snapshot()["active_process"] == 1
    assert limiter.snapshot()["active_keys"] == {
        "source": 1,
        "network": 1,
        "link": 1,
        "organization": 1,
    }

    lease.release()
    lease.release()
    assert limiter.snapshot()["active_process"] == 0
    assert limiter.snapshot()["active_keys"] == {
        "source": 0,
        "network": 0,
        "link": 0,
        "organization": 0,
    }


def test_depleted_aggregate_bucket_survives_reconnect_until_full():
    clock = _Clock()
    tiny = {scope: ByteLimit(10, 20) for scope in BYTE_LIMITS}
    limiter = _limiter(clock, byte=tiny)
    ticket = _resolved(limiter)
    first = limiter.acquire(ticket)
    assert first is not None
    assert first.charge_bytes(15)
    first.release()
    assert limiter.snapshot()["byte_bucket_keys"]["source"] == 1

    clock.advance(1.0)
    second = limiter.acquire(ticket)
    assert second is not None
    # Aggregate buckets have refilled only to 15; reconnect did not create
    # fresh 20-byte bursts even though the per-channel bucket is new.
    assert not second.charge_bytes(16)
    second.release()

    clock.advance(0.49)
    assert limiter.snapshot()["byte_bucket_keys"]["source"] == 1
    clock.advance(0.01)
    assert limiter.snapshot()["byte_bucket_keys"]["source"] == 0


def test_failed_multi_scope_byte_charge_does_not_debit_earlier_buckets():
    clock = _Clock()
    limits = {scope: ByteLimit(100, 200) for scope in BYTE_LIMITS}
    limits["process"] = ByteLimit(1, 1)
    limiter = _limiter(clock, byte=limits)
    lease = limiter.acquire(_resolved(limiter))
    assert lease is not None
    before = lease._channel_bucket.tokens
    assert not lease.charge_bytes(2)
    assert lease._channel_bucket.tokens == before
    lease.release()


def test_active_cap_fails_closed_without_allocating_exact_state():
    clock = _Clock()
    limiter = _limiter(clock, active={"process": 1})
    first = limiter.acquire(_resolved(limiter, "a"))
    assert first is not None
    assert limiter.acquire(_resolved(limiter, "b")) is None
    state = limiter.snapshot()
    assert state["active_process"] == 1
    assert state["decisions"]["process:active"] == 1
    first.release()


def test_limiter_state_contains_no_raw_source_token_org_or_composite_key():
    clock = _Clock()
    limiter = _limiter(clock)
    source = "203.0.113.77"
    token = "visible-token-that-must-not-remain"
    org = "77777777-7777-4777-8777-777777777777"
    pre = limiter.begin(source)
    assert pre is not None
    ticket = limiter.resolve(pre, token, org)
    assert ticket is not None
    lease = limiter.acquire(ticket)
    assert lease is not None

    rendered = repr(limiter.__dict__)
    assert source not in rendered
    assert token not in rendered
    assert org not in rendered
    assert f"{source}:{token}" not in rendered
    lease.release()


def test_invalid_limit_geometry_is_rejected():
    clock = _Clock()
    with pytest.raises(ValueError, match="burst <= sustained"):
        _limiter(
            clock,
            admission={"source": replace(ADMISSION_LIMITS["source"], burst=601)},
        )


def test_ten_thousand_source_attempts_cannot_grow_limiter_memory():
    clock = _Clock()
    limiter = _limiter(clock)
    allocated = limiter.admission_allocated_bytes
    for value in range(10_000):
        limiter.begin(f"2001:db8:{value >> 8:x}:{value & 0xff:x}::1")
    assert limiter.admission_allocated_bytes == allocated
    assert limiter.snapshot()["active_keys"] == {
        "source": 0,
        "network": 0,
        "link": 0,
        "organization": 0,
    }
    assert limiter.snapshot()["byte_bucket_keys"] == {
        "source": 0,
        "network": 0,
        "link": 0,
        "organization": 0,
    }


def test_shared_network_is_admitted_below_its_independent_allowance():
    clock = _Clock()
    limiter = _limiter(clock)
    for host in ("198.51.100.10", "198.51.100.11", "198.51.100.12"):
        assert limiter.begin(host) is not None
    assert "network:burst" not in limiter.snapshot()["decisions"]


def test_one_link_across_rotating_sources_hits_the_link_ceiling():
    clock = _Clock()
    limiter = _limiter(
        clock, admission={"link": AdmissionLimit(3, 30)}
    )
    for suffix in range(3):
        pre = limiter.begin(f"198.51.100.{10 + suffix}")
        assert pre is not None
        assert limiter.resolve(pre, "one-link", "one-org") is not None
    pre = limiter.begin("198.51.100.99")
    assert pre is not None
    assert limiter.resolve(pre, "one-link", "one-org") is None
    assert limiter.snapshot()["decisions"]["link:burst"] == 1


def test_ten_thousand_depleted_distinct_buckets_fail_closed_at_hard_bound():
    clock = _Clock()
    limiter = _limiter(clock)
    admitted = 0
    for value in range(10_000):
        key = value.to_bytes(32, "big")
        lease = limiter.acquire(ResolvedTicket(key, key, key, key))
        if lease is None:
            continue
        admitted += 1
        assert lease.charge_bytes(1)
        lease.release()

    assert admitted == 2_656
    assert limiter.snapshot()["byte_bucket_keys"] == {
        "source": 2_656,
        "network": 2_656,
        "link": 2_656,
        "organization": 2_656,
    }
    assert limiter.snapshot()["active_process"] == 0
    clock.advance(2.0)
    assert limiter.snapshot()["byte_bucket_keys"] == {
        "source": 0,
        "network": 0,
        "link": 0,
        "organization": 0,
    }


def _public_app(limiter: RelayAbuseLimiter) -> tuple[TestClient, str]:
    now = 1_800_000_000
    app = create_app(
        ":memory:",
        now_fn=lambda: now,
        secure_cookies=False,
        abuse_limiter=limiter,
    )
    token = "ab" * 16
    app.state.store.create_org(
        "11111111-1111-4111-8111-111111111111",
        "11" * 32,
        "none",
        None,
        now=now,
        expires_at=now + 3_600,
    )
    app.state.store.create_link(
        LinkGrant(
            token=token,
            org_uuid="11111111-1111-4111-8111-111111111111",
            target_uuid="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            target_type="present",
            meta={},
            created_at=now,
            expires_at=now + 3_600,
            revoked_at=None,
            signer_pub="22" * 32,
            subject_kind="operator",
            subject_id="op",
        )
    )
    return TestClient(app), token


def test_http_rate_refusals_reuse_unknown_link_status_and_body():
    boot_clock = _Clock()
    boot_limiter = _limiter(
        boot_clock, admission={"source": AdmissionLimit(1, 10)}
    )
    boot_client, token = _public_app(boot_limiter)
    live = boot_client.get(f"/l/{token}")
    refused = boot_client.get(f"/l/{token}")
    assert live.status_code == 200
    assert refused.status_code == 404
    assert refused.content == live.content

    envelope_clock = _Clock()
    envelope_limiter = _limiter(
        envelope_clock, admission={"source": AdmissionLimit(1, 10)}
    )
    envelope_client, token = _public_app(envelope_limiter)
    assert envelope_client.get(f"/v1/links/{token}/envelope").status_code == 200
    refused = envelope_client.get(f"/v1/links/{token}/envelope")
    unknown_client, _ = _public_app(_limiter(_Clock()))
    unknown_shape = unknown_client.get("/v1/links/unknown/envelope")
    assert refused.status_code == unknown_shape.status_code == 404
    assert refused.content == unknown_shape.content


class _TunnelSocket:
    def __init__(self) -> None:
        self.frames: list[bytes] = []

    async def send_bytes(self, payload: bytes) -> None:
        self.frames.append(bytes(payload))


class _ViewerSocket:
    def __init__(self) -> None:
        self.client = SimpleNamespace(host="198.51.100.8")
        self.close_codes: list[int] = []
        self.accepted = False

    async def accept(self) -> None:
        self.accepted = True

    async def send_bytes(self, payload: bytes) -> None:
        raise AssertionError("rate-refused payload must not reach the viewer")

    async def receive(self) -> dict:
        return {"type": "websocket.receive", "bytes": b"xx"}

    async def close(self, *, code: int) -> None:
        self.close_codes.append(code)


def test_outbound_byte_refusal_closes_uniformly_and_releases_active_state():
    async def run() -> None:
        clock = _Clock()
        tiny = {scope: ByteLimit(1, 1) for scope in BYTE_LIMITS}
        limiter = _limiter(clock, byte=tiny)
        lease = limiter.acquire(_resolved(limiter))
        assert lease is not None
        socket = _ViewerSocket()
        tunnel = Tunnel(_TunnelSocket(), "org")
        channel_id = b"c" * 16
        tunnel.add_viewer(channel_id, socket, abuse_lease=lease)

        tunnel.enqueue_viewer(channel_id, b"xx")
        for _ in range(10):
            if socket.close_codes:
                break
            await asyncio.sleep(0)
        assert socket.close_codes == [CLOSE_UNKNOWN_LINK]
        assert channel_id not in tunnel.channels
        assert limiter.snapshot()["active_process"] == 0
        await tunnel.close_all_viewers(1001)

    asyncio.run(run())


def test_inbound_byte_refusal_never_enters_dashboard_queue(monkeypatch):
    async def run() -> None:
        clock = _Clock()
        tiny = {scope: ByteLimit(1, 1) for scope in BYTE_LIMITS}
        limiter = _limiter(clock, byte=tiny)
        tunnel_socket = _TunnelSocket()
        tunnel = Tunnel(tunnel_socket, "org")

        class _Hub:
            def get(self, org: str):
                return tunnel

        class _Link:
            org_uuid = "org"

        monkeypatch.setattr(
            relay, "_resolve_live_link", lambda store, token, now: _Link()
        )
        socket = _ViewerSocket()
        await viewer_endpoint(
            socket,
            "ab" * 16,
            _Hub(),
            None,
            lambda: 0,
            abuse_limiter=limiter,
        )

        from tools.network.relaykit.frames import FRAME_DATA, decode_frame

        assert socket.accepted
        assert not any(decode_frame(frame).type == FRAME_DATA for frame in tunnel_socket.frames)
        assert limiter.snapshot()["active_process"] == 0

    asyncio.run(run())


def test_listener_attachment_failure_releases_channel_lease(monkeypatch):
    async def run() -> None:
        clock = _Clock()
        limiter = _limiter(clock)
        tunnel = Tunnel(_TunnelSocket(), "org")

        class _Hub:
            def get(self, org: str):
                return tunnel

        class _Link:
            org_uuid = "org"

        monkeypatch.setattr(
            relay, "_resolve_live_link", lambda store, token, now: _Link()
        )
        monkeypatch.setattr(
            tunnel,
            "attach_listener",
            lambda token, channel_id, channel: (_ for _ in ()).throw(
                RuntimeError("injected attachment failure")
            ),
        )
        socket = _ViewerSocket()
        await viewer_endpoint(
            socket,
            "ab" * 16,
            _Hub(),
            None,
            lambda: 0,
            abuse_limiter=limiter,
        )

        assert socket.close_codes == [CLOSE_UNKNOWN_LINK]
        assert tunnel.channels == {}
        assert limiter.snapshot()["active_process"] == 0

    asyncio.run(run())
