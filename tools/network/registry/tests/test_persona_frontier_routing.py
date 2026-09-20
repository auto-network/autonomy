"""auto-xs9hz: route links by advertised per-persona frontiers.

A link carries R, the author personas of its rows with timestamps. Each
serving member advertises one frontier per member persona (ctrl op
sync-frontier). The relay dials only members whose frontiers cover every
entry of R, orders the publisher first inside the fresh-link pin window,
and least-loaded after it; the pin orders, it never refuses
(graph://d9153c5a-76e O-C, graph://6ad52a52-f75 principle 2).
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from tools.network.registry import relay as relay_mod
from tools.network.registry.relay import (
    CLOSE_NO_COVERING_MEMBER, FRESH_LINK_PIN_S, PERSONA_MAP_MAX, Tunnel, TunnelHub,
    viewer_endpoint,
)
from tools.network.relaykit.close_codes import MEANINGS

from .conftest import ORG, TARGET, register
from .test_machine_pinned_routing import _TunnelSocket, _ViewerSocket, _ctrl, _opened, _tunnel

P1, P2 = "11" * 32, "22" * 32
MACHINE_A, MACHINE_B = "aa" * 32, "bb" * 32


def _link(requires):
    class _Link:
        org_uuid = ORG
        target_type = "note"
        serving_machine = None
    _Link.requires = requires
    return _Link()


def _pool():
    hub = TunnelHub()
    a_sock, b_sock = _TunnelSocket(), _TunnelSocket()
    a = Tunnel(a_sock, ORG, persona_pub=P1, machine=MACHINE_A)
    b = Tunnel(b_sock, ORG, persona_pub=P2, machine=MACHINE_B)
    hub.register(a); hub.register(b)
    return hub, (a, a_sock), (b, b_sock)


def _dial(hub, token, now=1_010.0):
    ws = _ViewerSocket()
    asyncio.run(viewer_endpoint(ws, token, hub, None, lambda: now))
    return ws


# ── selection ────────────────────────────────────────────────────────

def test_a_covering_member_is_preferred_over_a_less_loaded_one_that_is_behind(monkeypatch):
    hub, (a, a_sock), (b, b_sock) = _pool()
    a.frontiers = {P1: 100}                    # covers the link
    b.frontiers = {P1: 50}                     # behind on P1
    a.channels[b"x" * 16] = object()           # least-loaded would pick b
    monkeypatch.setattr(relay_mod, "_resolve_live_link", lambda s, t, n: _link({P1: 80}))
    _dial(hub, "a" * 32)
    assert _opened(a_sock) and not _opened(b_sock)


def test_a_member_behind_on_one_listed_persona_is_never_dialed(monkeypatch):
    hub, (a, a_sock), (b, b_sock) = _pool()
    a.frontiers = {P1: 100, P2: 10}            # current on P1, behind on P2
    b.frontiers = {P1: 100, P2: 100}
    monkeypatch.setattr(relay_mod, "_resolve_live_link", lambda s, t, n: _link({P1: 80, P2: 50}))
    _dial(hub, "b" * 32)
    assert _opened(b_sock) and not _opened(a_sock)


def test_no_covering_member_and_no_publisher_closes_4431_with_a_meaning(monkeypatch):
    hub, (a, a_sock), (b, b_sock) = _pool()
    a.frontiers = {P1: 10}
    b.frontiers = {}
    monkeypatch.setattr(relay_mod, "_resolve_live_link", lambda s, t, n: _link({P1: 80}))
    ws = _dial(hub, "c" * 32)
    assert ws.close_codes == [CLOSE_NO_COVERING_MEMBER]
    assert not _opened(a_sock) and not _opened(b_sock)
    assert CLOSE_NO_COVERING_MEMBER in MEANINGS


def test_the_publisher_is_dialed_first_inside_the_pin_window_even_before_its_cut_covers(monkeypatch):
    hub, (a, a_sock), (b, b_sock) = _pool()
    a.frontiers = {P1: 50}                     # the publisher; its persona cut lags its write
    b.frontiers = {P1: 100}                    # a covering member
    monkeypatch.setattr(relay_mod, "_resolve_live_link", lambda s, t, n: _link({P1: 80}))
    token = "d" * 32
    hub.pin_fresh_link(token, MACHINE_A, now=1_000.0)
    _dial(hub, token, now=1_010.0)
    assert _opened(a_sock) and not _opened(b_sock)
    # After the window only covering members are candidates.
    hub2, (a2, a2_sock), (b2, b2_sock) = _pool()
    a2.frontiers = {P1: 50}; b2.frontiers = {P1: 100}
    hub2.pin_fresh_link(token, MACHINE_A, now=1_000.0)
    _dial(hub2, token, now=1_000.0 + FRESH_LINK_PIN_S + 1)
    assert _opened(b2_sock) and not _opened(a2_sock)


def test_a_link_without_requirements_routes_as_before(monkeypatch):
    hub, (a, a_sock), (b, b_sock) = _pool()
    a.channels[b"x" * 16] = object()
    monkeypatch.setattr(relay_mod, "_resolve_live_link", lambda s, t, n: _link(None))
    _dial(hub, "e" * 32)
    assert _opened(b_sock) and not _opened(a_sock)   # least-loaded


# ── the advert and the link row, over the real tunnel ─────────────────

def test_sync_frontier_sets_the_tunnels_map_and_refuses_bad_shapes(app, client, clock, root):
    register(client, clock, root, org_uuid=ORG)
    with _tunnel(client, clock, root, caps=()) as (ws, machine):
        reply = _ctrl(ws, "sync-frontier", {"org_uuid": ORG, "frontiers": {P1: 5, P2: 7}})
        assert reply["ok"] is True and reply["personas"] == 2
        tunnel = next(t for t in app.state.hub.tunnels_for(ORG) if t.machine == machine)
        assert tunnel.frontiers == {P1: 5, P2: 7}
        assert _ctrl(ws, "sync-frontier", {"org_uuid": "other", "frontiers": {}})["ok"] is False
        assert _ctrl(ws, "sync-frontier", {"org_uuid": ORG, "frontiers": {"nope": 1}})["ok"] is False
        assert _ctrl(ws, "sync-frontier", {"org_uuid": ORG, "frontiers": {P1: -1}})["ok"] is False
        assert _ctrl(ws, "sync-frontier", {"org_uuid": ORG})["ok"] is False
        assert tunnel.frontiers == {P1: 5, P2: 7}, "a refused advert leaves the map alone"


def test_an_oversized_advert_closes_the_tunnel_4406(app, client, clock, root):
    from tools.network.relaykit.frames import CTRL_CHANNEL_ID, FRAME_CTRL, encode_frame
    register(client, clock, root, org_uuid=ORG)
    with _tunnel(client, clock, root, caps=()) as (ws, machine):
        too_many = {format(i, "064x"): 1 for i in range(PERSONA_MAP_MAX + 1)}
        ws.send_bytes(encode_frame(
            FRAME_CTRL, CTRL_CHANNEL_ID,
            json.dumps({"id": "0" * 32, "op": "sync-frontier",
                        "args": {"org_uuid": ORG, "frontiers": too_many}}).encode(),
        ))
        with pytest.raises(Exception):
            ws.receive_bytes()
        deadline = time.time() + 2
        while time.time() < deadline and app.state.hub.tunnels_for(ORG):
            time.sleep(0.02)
        assert app.state.hub.tunnels_for(ORG) == []


def test_create_link_stores_requires_as_persona_keys_and_integers_only(app, client, clock, root):
    register(client, clock, root, org_uuid=ORG)
    with _tunnel(client, clock, root, caps=()) as (ws, machine):
        ok = _ctrl(ws, "create-link", {
            "target_uuid": TARGET, "target_type": "note", "requires": {P2: 40, P1: 30},
        })
        assert ok["ok"] is True, ok
        grant = app.state.store.get_link(ok["token"])
        assert grant.requires == {P1: 30, P2: 40}
        raw = app.state.store._conn.execute(
            "SELECT requires FROM links WHERE token=?", (ok["token"],)
        ).fetchone()[0]
        assert json.loads(raw) == {P1: 30, P2: 40}
        for bad in ({"x": 1}, {P1: "1"}, {P1: -5}, [P1]):
            refused = _ctrl(ws, "create-link", {
                "target_uuid": TARGET, "target_type": "note", "requires": bad,
            })
            assert refused["ok"] is False, bad
        plain = _ctrl(ws, "create-link", {"target_uuid": TARGET, "target_type": "note"})
        assert plain["ok"] is True and app.state.store.get_link(plain["token"]).requires is None


def test_create_link_carries_the_grant_id_and_hands_it_to_the_member(app, client, clock, root):
    """O-C (2026-09-20): R and the publisher-minted grant id ride in the one
    create-link; the registry stores the id on the link. A link without one
    is resolved by its token."""
    register(client, clock, root, org_uuid=ORG)
    with _tunnel(client, clock, root, caps=()) as (ws, machine):
        made = _ctrl(ws, "create-link", {
            "target_uuid": TARGET, "target_type": "note",
            "requires": {P1: 12}, "grant_id": "ab" * 16,
        })
        assert made["ok"] is True
        link = app.state.store.get_link(made["token"])
        assert link.requires == {P1: 12} and link.grant_id == "ab" * 16
        for bad in ("short", "AB" * 16, 7):
            refused = _ctrl(ws, "create-link", {
                "target_uuid": TARGET, "target_type": "note", "grant_id": bad,
            })
            assert refused["ok"] is False, bad
        plain = _ctrl(ws, "create-link", {"target_uuid": TARGET, "target_type": "note"})
        assert app.state.store.get_link(plain["token"]).grant_id is None
        assert _ctrl(ws, "set-link-requires", {"token": made["token"], "requires": {P1: 1}})["ok"] is False
