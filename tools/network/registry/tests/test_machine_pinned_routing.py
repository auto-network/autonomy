"""auto-nh1po: machine-targeted serve routing (graph://96a4aa40-1c9).

A serve link points at a live port on ONE machine; a ``design``/``present``
/``mission`` share link points at a store that does not fleet-sync. The
publisher declares the serving machine, the registry records it, and the
relay routes only to the tunnel whose authenticated hello named that
machine. Every check here is against ``tunnel.machine`` — the hello's
co-signed serving machine key — never an op-body identity.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import sqlite3
import uuid

import pytest

from tools.network.idkit import KeyPair, Subject, issue_cert
from tools.network.registry import relay as relay_mod
from tools.network.registry.relay import CLOSE_SERVING_MACHINE_OFFLINE, CLOSE_UNKNOWN_LINK, Tunnel, viewer_endpoint
from tools.network.registry.store import LinkGrant, RegistryStore
from tools.network.relaykit import hello as hello_mod
from tools.network.relaykit.frames import (
    CTRL_CHANNEL_ID,
    FRAME_CTRL,
    FRAME_OPEN,
    decode_frame,
    encode_frame,
)

from .conftest import DAY, NOW, ORG, TARGET, register

PERSONA_A = "ab" * 32
RESERVATION_NS = uuid.UUID("6cf440db-c8b4-566c-99db-e7be17109bdc")


def _label(persona_pub: str, slug: str = "worker") -> str:
    suffix = hashlib.sha256(bytes.fromhex(persona_pub)).hexdigest()[:20]
    return f"{slug}-{suffix}"


def _host(app_label: str, persona_pub: str = PERSONA_A) -> str:
    return f"{app_label}.{_label(persona_pub)}.serve.auto.network"


def _reservation(app_label: str, persona_pub: str = PERSONA_A) -> str:
    return str(uuid.uuid5(RESERVATION_NS, f"{persona_pub}\0{app_label}"))


def _serve_cert(root, serve_key, persona=PERSONA_A):
    return issue_cert(
        root, serve_key.public_hex, scope=("tunnel:serve",), org=ORG,
        subject=Subject("persona", persona),
        not_before=NOW - 100, not_after=NOW + 30 * DAY,
    )


@contextlib.contextmanager
def _tunnel(client, clock, root, *, machine_key=None, caps=("host-lease/1",)):
    serve_key = KeyPair.generate()
    machine_key = machine_key or KeyPair.generate()
    raw = hello_mod.build_tunnel_hello_v2(
        serve_key, _serve_cert(root, serve_key), machine_key=machine_key,
        org=ORG, ts=clock.now, caps=caps,
        machine_hello_domain=hello_mod.SERVING_MACHINE_HELLO_DOMAIN,
    )
    with client.websocket_connect(f"/t/{ORG}") as ws:
        ws.send_text(raw)
        ack = ws.receive_json()
        assert ack["ok"] is True, ack
        yield ws, machine_key.public_hex


_SEQ = iter(range(100_000))


def _ctrl(ws, op, args):
    correlation = format(next(_SEQ), "032x")
    ws.send_bytes(encode_frame(
        FRAME_CTRL, CTRL_CHANNEL_ID,
        json.dumps({"id": correlation, "op": op, "args": args}).encode(),
    ))
    frame = decode_frame(ws.receive_bytes())
    assert frame.type == FRAME_CTRL
    reply = json.loads(frame.payload.decode())
    assert reply["id"] == correlation
    return reply


# ── store ────────────────────────────────────────────────────────────


def test_store_round_trips_serving_machine_and_defaults_to_org_wide():
    store = RegistryStore(":memory:")
    pinned = LinkGrant(
        token="a" * 32, org_uuid=ORG, target_uuid=TARGET, target_type="present",
        meta={}, created_at=NOW, expires_at=None, revoked_at=None,
        signer_pub=None, subject_kind="org-tunnel", subject_id=None,
        serving_machine="ef" * 32,
    )
    wide = LinkGrant(
        token="b" * 32, org_uuid=ORG, target_uuid=TARGET, target_type="note",
        meta={}, created_at=NOW, expires_at=None, revoked_at=None,
        signer_pub=None, subject_kind="org-tunnel", subject_id=None,
    )
    store.create_link(pinned)
    store.create_link(wide)
    assert store.get_link("a" * 32).serving_machine == "ef" * 32
    assert store.get_link("b" * 32).serving_machine is None
    store.close()


def test_store_migrates_pre_pinning_tables(tmp_path):
    """An existing registry (links without serving_machine, serve_hosts
    without machine) gains both columns on open; rows survive."""
    path = tmp_path / "registry.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE links (
            token TEXT PRIMARY KEY, org_uuid TEXT NOT NULL,
            target_uuid TEXT NOT NULL, target_type TEXT NOT NULL,
            invite_ref TEXT, meta TEXT NOT NULL, created_at INTEGER NOT NULL,
            expires_at INTEGER, expires_at_ms INTEGER, revoked_at INTEGER,
            signer_pub TEXT, subject_kind TEXT NOT NULL, subject_id TEXT,
            operation_id TEXT
        );
        CREATE TABLE serve_hosts (
            reservation_id TEXT PRIMARY KEY, org_uuid TEXT NOT NULL,
            persona_pub TEXT NOT NULL, host TEXT NOT NULL UNIQUE,
            generation INTEGER NOT NULL, created_at INTEGER NOT NULL
        );
        INSERT INTO serve_hosts VALUES ('r1', 'o', 'p', 'h.example', 3, 1);
        """
    )
    conn.commit()
    conn.close()

    store = RegistryStore(str(path))
    link_cols = {r["name"] for r in store._conn.execute("PRAGMA table_info(links)")}
    host_cols = {r["name"] for r in store._conn.execute("PRAGMA table_info(serve_hosts)")}
    assert "serving_machine" in link_cols
    assert "machine" in host_cols
    owner = store.get_host_ownership("r1")
    assert owner.generation == 3 and owner.machine is None
    store.close()


def test_upsert_host_ownership_keeps_a_declaration_across_undeclared_refresh():
    store = RegistryStore(":memory:")
    store.upsert_host_ownership(
        reservation_id="r", org=ORG, persona_pub=PERSONA_A, host="h",
        now=NOW, machine="ef" * 32,
    )
    store.upsert_host_ownership(
        reservation_id="r", org=ORG, persona_pub=PERSONA_A, host="h", now=NOW,
    )
    assert store.get_host_ownership("r").machine == "ef" * 32
    store.clear_host_ownership_machine("r")
    assert store.get_host_ownership("r").machine is None
    store.close()


# ── host-register: the declared machine ──────────────────────────────


def test_declared_machine_is_recorded_and_must_be_the_tunnels_own(
    app, client, clock, root,
):
    register(client, clock, root, org_uuid=ORG)
    host, res = _host("app"), _reservation("app")
    with _tunnel(client, clock, root) as (ws, machine):
        # A declaration naming a machine this tunnel did not authenticate is
        # a wrong-machine connector (or a lie): refused before any write.
        reply = _ctrl(ws, "host-register", {
            "reservation": res, "host": host, "machine": "ef" * 32,
        })
        assert reply["ok"] is False and reply["error"] == "host-owned-elsewhere"
        assert app.state.store.get_host_ownership(res) is None
        # Malformed declarations are a bad request, not an ownership verdict.
        reply = _ctrl(ws, "host-register", {
            "reservation": res, "host": host, "machine": "not-hex",
        })
        assert reply["ok"] is False and reply["error"] == "bad-request"

        reply = _ctrl(ws, "host-register", {
            "reservation": res, "host": host, "machine": machine,
        })
        assert reply["ok"] is True, reply
        assert app.state.store.get_host_ownership(res).machine == machine
        assert app.state.host_routes.route(host) is not None


def test_pinned_host_refuses_a_sibling_machine_even_after_the_lease_lapses(
    app, client, clock, root,
):
    """The 2026-09-10 split: two same-persona connectors trading the lease
    for a service only one of them runs. Once machine A declared the host,
    machine B's (undeclared) register is refused while A is live AND after
    A's lease is gone — a lapsed lease is not an invitation."""
    register(client, clock, root, org_uuid=ORG)
    host, res = _host("app"), _reservation("app")
    key_a, key_b = KeyPair.generate(), KeyPair.generate()
    with _tunnel(client, clock, root, machine_key=key_a) as (ws_a, machine_a):
        assert _ctrl(ws_a, "host-register", {
            "reservation": res, "host": host, "machine": machine_a,
        })["ok"] is True
        with _tunnel(client, clock, root, machine_key=key_b) as (ws_b, machine_b):
            reply = _ctrl(ws_b, "host-register", {"reservation": res, "host": host})
            assert reply["ok"] is False
            assert reply["error"] == "host-owned-elsewhere"
            # Declaring ITSELF does not help B: the pin is A's until released.
            reply = _ctrl(ws_b, "host-register", {
                "reservation": res, "host": host, "machine": machine_b,
            })
            assert reply["error"] == "host-owned-elsewhere"
    # A is gone (tunnel closed -> lease dropped) but the pin stays.
    assert app.state.host_routes.route(host) is None
    with _tunnel(client, clock, root, machine_key=key_b) as (ws_b, machine_b):
        reply = _ctrl(ws_b, "host-register", {
            "reservation": res, "host": host, "machine": machine_b,
        })
        assert reply["ok"] is False and reply["error"] == "host-owned-elsewhere"
        assert app.state.store.get_host_ownership(res).machine == machine_a
    # A comes back and simply re-registers: same machine, still its host.
    with _tunnel(client, clock, root, machine_key=key_a) as (ws_a, machine_a):
        assert _ctrl(ws_a, "host-register", {
            "reservation": res, "host": host, "machine": machine_a,
        })["ok"] is True


def test_explicit_release_moves_the_pin_but_never_displaces_a_live_lease(
    app, client, clock, root,
):
    register(client, clock, root, org_uuid=ORG)
    host, res = _host("app"), _reservation("app")
    key_a, key_b = KeyPair.generate(), KeyPair.generate()
    with _tunnel(client, clock, root, machine_key=key_a) as (ws_a, machine_a):
        assert _ctrl(ws_a, "host-register", {
            "reservation": res, "host": host, "machine": machine_a,
        })["ok"] is True
        with _tunnel(client, clock, root, machine_key=key_b) as (ws_b, machine_b):
            # B cannot release out from under a live A.
            reply = _ctrl(ws_b, "host-release", {"reservation": res})
            assert reply["ok"] is False and reply["error"] == "lease-held"
            assert app.state.store.get_host_ownership(res).machine == machine_a
    # A is dead and the service moved to B: B releases (same persona, no
    # live lease), which clears the pin, then declares itself.
    with _tunnel(client, clock, root, machine_key=key_b) as (ws_b, machine_b):
        assert _ctrl(ws_b, "host-release", {"reservation": res})["ok"] is True
        assert app.state.store.get_host_ownership(res).machine is None
        assert _ctrl(ws_b, "host-register", {
            "reservation": res, "host": host, "machine": machine_b,
        })["ok"] is True
        assert app.state.store.get_host_ownership(res).machine == machine_b
        # And now A is the stranger.
        with _tunnel(client, clock, root, machine_key=key_a) as (ws_a, machine_a):
            reply = _ctrl(ws_a, "host-register", {
                "reservation": res, "host": host, "machine": machine_a,
            })
            assert reply["error"] == "host-owned-elsewhere"


def test_undeclared_register_keeps_persona_only_semantics(app, client, clock, root):
    """A connector that predates the declaration registers exactly as
    before: persona ownership, no pin, a sibling may take a lapsed lease."""
    register(client, clock, root, org_uuid=ORG)
    host, res = _host("app"), _reservation("app")
    with _tunnel(client, clock, root) as (ws_a, _):
        assert _ctrl(ws_a, "host-register", {"reservation": res, "host": host})["ok"] is True
        assert app.state.store.get_host_ownership(res).machine is None
    with _tunnel(client, clock, root) as (ws_b, _):
        assert _ctrl(ws_b, "host-register", {"reservation": res, "host": host})["ok"] is True


# ── create-link: the declared serving machine ────────────────────────


def test_create_link_pins_to_the_tunnels_machine_and_refuses_any_other(
    app, client, clock, root,
):
    register(client, clock, root, org_uuid=ORG)
    with _tunnel(client, clock, root, caps=()) as (ws, machine):
        reply = _ctrl(ws, "create-link", {
            "target_uuid": TARGET, "target_type": "present",
            "serving_machine": "ef" * 32,
        })
        assert reply["ok"] is False
        assert "not the machine this tunnel authenticated" in reply["error"]

        reply = _ctrl(ws, "create-link", {
            "target_uuid": TARGET, "target_type": "present",
            "serving_machine": "nope",
        })
        assert reply["ok"] is False and "64 lowercase hex" in reply["error"]

        pinned = _ctrl(ws, "create-link", {
            "target_uuid": TARGET, "target_type": "present",
            "serving_machine": machine,
        })
        assert pinned["ok"] is True, pinned
        assert app.state.store.get_link(pinned["token"]).serving_machine == machine

        wide = _ctrl(ws, "create-link", {
            "target_uuid": TARGET, "target_type": "note",
        })
        assert wide["ok"] is True
        assert app.state.store.get_link(wide["token"]).serving_machine is None


# ── viewer routing ───────────────────────────────────────────────────


class _TunnelSocket:
    def __init__(self):
        self.frames: list[bytes] = []

    async def send_bytes(self, payload: bytes) -> None:
        self.frames.append(bytes(payload))


class _ViewerSocket:
    def __init__(self):
        self.accepted = False
        self.close_codes: list[int] = []

    async def accept(self):
        self.accepted = True

    async def receive(self):
        return {"type": "websocket.disconnect"}

    async def close(self, *, code: int, reason: str = ""):
        self.close_codes.append(code)


class _Hub:
    def __init__(self, *tunnels):
        self._tunnels = list(tunnels)
        self.least_loaded_calls = 0

    def get(self, org):
        self.least_loaded_calls += 1
        return min(self._tunnels, key=lambda t: len(t.channels)) if self._tunnels else None

    def tunnels_for(self, org):
        return list(self._tunnels)


def _link(serving_machine):
    class _Link:
        org_uuid = ORG
        target_type = "present"
    _Link.serving_machine = serving_machine
    return _Link()


def _opened(sock: _TunnelSocket) -> bool:
    return any(decode_frame(f).type == FRAME_OPEN for f in sock.frames)


def test_pinned_link_routes_only_to_the_declared_machine(monkeypatch):
    sock_x, sock_y = _TunnelSocket(), _TunnelSocket()
    tunnel_x = Tunnel(sock_x, ORG, machine="11" * 32)
    tunnel_y = Tunnel(sock_y, ORG, machine="22" * 32)
    # X is the least-loaded tunnel; the pin must still choose Y.
    tunnel_y.channels = {b"a" * 16: object(), b"b" * 16: object()}
    hub = _Hub(tunnel_x, tunnel_y)
    monkeypatch.setattr(relay_mod, "_resolve_live_link",
                        lambda store, token, now: _link("22" * 32))

    ws = _ViewerSocket()
    asyncio.run(viewer_endpoint(ws, "0" * 32, hub, None, lambda: 0))

    assert ws.accepted
    assert _opened(sock_y) and not _opened(sock_x)
    assert hub.least_loaded_calls == 0


def test_pinned_link_with_its_machine_offline_says_so(monkeypatch):
    sock_x = _TunnelSocket()
    hub = _Hub(Tunnel(sock_x, ORG, machine="11" * 32))
    monkeypatch.setattr(relay_mod, "_resolve_live_link",
                        lambda store, token, now: _link("22" * 32))

    ws = _ViewerSocket()
    asyncio.run(viewer_endpoint(ws, "0" * 32, hub, None, lambda: 0))

    assert ws.close_codes == [CLOSE_SERVING_MACHINE_OFFLINE]
    assert not _opened(sock_x)  # never handed to a machine without the content


def test_org_wide_link_still_takes_the_least_loaded_tunnel(monkeypatch):
    sock_x, sock_y = _TunnelSocket(), _TunnelSocket()
    tunnel_x = Tunnel(sock_x, ORG, machine="11" * 32)
    tunnel_y = Tunnel(sock_y, ORG, machine="22" * 32)
    tunnel_y.channels = {b"a" * 16: object()}
    hub = _Hub(tunnel_x, tunnel_y)
    monkeypatch.setattr(relay_mod, "_resolve_live_link",
                        lambda store, token, now: _link(None))

    ws = _ViewerSocket()
    asyncio.run(viewer_endpoint(ws, "0" * 32, hub, None, lambda: 0))

    assert hub.least_loaded_calls == 1
    assert _opened(sock_x) and not _opened(sock_y)


# ── fresh-link pin: a just-published link routes to its publisher ─────────
#
# Its grant lives in the publishing machine's grant cache until org sync
# replicates it; routed anywhere else in the pool it got "link unavailable"
# from a connector that could not know it yet (dynbench, 2026-09-15: the
# publish probe landed on the fleet machine's tunnel). Volatile, in-memory,
# five minutes, swept hourly.

from tools.network.registry.relay import (  # noqa: E402
    CLOSE_PUBLISHER_OFFLINE, FRESH_LINK_PIN_S, FRESH_LINK_SWEEP_S, TunnelHub,
)


def _pool(now_holder):
    """A real hub with two tunnels for ORG: the publisher (machine 'aa'…)
    and another member machine ('bb'…)."""
    hub = TunnelHub()
    publisher, other = _TunnelSocket(), _TunnelSocket()
    hub.register(Tunnel(publisher, ORG, persona_pub="11" * 32, machine="aa" * 32))
    hub.register(Tunnel(other, ORG, persona_pub="22" * 32, machine="bb" * 32))
    return hub, publisher, other


def test_a_fresh_link_routes_only_to_its_publisher(monkeypatch):
    hub, publisher, other = _pool(None)
    monkeypatch.setattr(relay_mod, "_resolve_live_link", lambda store, token, now: _link(None))
    token = "f" * 32
    hub.pin_fresh_link(token, "aa" * 32, now=1_000.0)
    # Load the publisher so least-loaded would have picked the other tunnel.
    hub.get_slot(ORG, "11" * 32, "aa" * 32).channels[b"x" * 16] = object()

    ws = _ViewerSocket()
    asyncio.run(viewer_endpoint(ws, token, hub, None, lambda: 1_010))
    assert _opened(publisher) and not _opened(other)


def test_after_the_window_the_pool_rule_returns(monkeypatch):
    hub, publisher, other = _pool(None)
    monkeypatch.setattr(relay_mod, "_resolve_live_link", lambda store, token, now: _link(None))
    token = "e" * 32
    hub.pin_fresh_link(token, "aa" * 32, now=1_000.0)
    hub.get_slot(ORG, "11" * 32, "aa" * 32).channels[b"x" * 16] = object()

    ws = _ViewerSocket()
    asyncio.run(viewer_endpoint(ws, token, hub, None, lambda: 1_000 + int(FRESH_LINK_PIN_S) + 1))
    assert _opened(other) and not _opened(publisher)   # least-loaded again
    assert hub.fresh_link_machine(token, 2_000.0) is None  # dropped on lookup


def test_publisher_offline_inside_the_window_tries_the_pool(monkeypatch):
    """The pin ORDERS candidates; it never refuses (graph://6ad52a52-f75
    principle 2). With the publisher absent the rest of the pool is tried,
    and a member that lacks the grant refuses with its own code, which the
    failover path handles (tests below)."""
    hub = TunnelHub()
    other = _TunnelSocket()
    hub.register(Tunnel(other, ORG, persona_pub="22" * 32, machine="bb" * 32))
    monkeypatch.setattr(relay_mod, "_resolve_live_link", lambda store, token, now: _link(None))
    token = "d" * 32
    hub.pin_fresh_link(token, "aa" * 32, now=1_000.0)   # publisher has no tunnel

    ws = _ViewerSocket()
    asyncio.run(viewer_endpoint(ws, token, hub, None, lambda: 1_010))
    assert _opened(other)
    assert ws.close_codes == [1001]


def test_fresh_link_pins_are_swept_hourly_on_the_create_path():
    hub = TunnelHub()
    for i in range(5):
        hub.pin_fresh_link(f"{i:032x}", "aa" * 32, now=0.0)
    assert len(hub._fresh_links) == 5
    # Publishing again inside the hour does not sweep; after an hour it does,
    # and only the entries whose window has passed are dropped.
    hub.pin_fresh_link("5" * 32, "aa" * 32, now=FRESH_LINK_SWEEP_S - 10)
    assert len(hub._fresh_links) == 6
    hub.pin_fresh_link("6" * 32, "aa" * 32, now=FRESH_LINK_SWEEP_S)
    assert set(hub._fresh_links) == {"5" * 32, "6" * 32}   # the five expired ones are gone


def test_a_machine_pinned_link_is_never_fresh_pinned():
    """Machine-local links keep their permanent pin; the fresh pin is only
    for org-wide links whose grant has not replicated yet."""
    hub = TunnelHub()
    hub.pin_fresh_link("c" * 32, None, now=0.0)   # no machine: nothing recorded
    assert hub._fresh_links == {}


# ── viewer failover across the org pool (graph://d9153c5a-76e O-B, auto-s81lo) ─
#
# A member that refuses BEFORE serving a byte (unarmed, no key for this
# link, closed empty, at its cap, tunnel torn down) hands the viewer to the
# next candidate; the viewer's client hello is replayed; the open budget
# starts only once the viewer's first frame was forwarded (the channel is
# viewer-first). 2026-09-17: SJC, 35 h behind, refused half of dynbench's
# viewer loads with 4502 while Home sat idle.

from tools.network.relaykit.close_codes import (  # noqa: E402
    CLOSE_CONNECTOR_UNARMED, CLOSE_KEY_RESOLUTION_REFUSED,
    CLOSE_VIEWER_HANDSHAKE_FAILED,
)
from tools.network.relaykit.frames import FRAME_CLOSE, FRAME_DATA  # noqa: E402


class _LiveViewerSocket:
    """A viewer that sends one client-hello frame, then stays connected until
    the test releases it."""

    def __init__(self, hello=b"client-hello"):
        self.accepted = False
        self.close_codes: list[int] = []
        self.close_reasons: list[str] = []
        self._hello = hello
        self._sent_hello = False
        self.release = asyncio.Event()

    async def accept(self):
        self.accepted = True

    async def receive(self):
        if not self._sent_hello:
            self._sent_hello = True
            return {"type": "websocket.receive", "bytes": self._hello}
        await self.release.wait()
        return {"type": "websocket.disconnect"}

    async def close(self, *, code: int, reason: str = ""):
        self.close_codes.append(code)
        self.close_reasons.append(reason)

    async def send_bytes(self, payload: bytes) -> None:
        pass


def _frames(sock: _TunnelSocket, frame_type):
    return [decode_frame(f) for f in sock.frames if decode_frame(f).type == frame_type]


async def _settle(predicate, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.005)


def _two(monkeypatch, serving_machine=None):
    hub = TunnelHub()
    first, second = _TunnelSocket(), _TunnelSocket()
    t1 = Tunnel(first, ORG, persona_pub="11" * 32, machine="aa" * 32)
    t2 = Tunnel(second, ORG, persona_pub="22" * 32, machine="bb" * 32)
    hub.register(t1)
    hub.register(t2)
    # Least-loaded picks t1 first: load t2.
    t2.channels[b"z" * 16] = object()
    monkeypatch.setattr(relay_mod, "_resolve_live_link",
                        lambda store, token, now: _link(serving_machine))
    return hub, (first, t1), (second, t2)


def test_a_member_that_refuses_before_serving_hands_the_viewer_to_the_next(monkeypatch):
    async def run():
        hub, (s1, t1), (s2, t2) = _two(monkeypatch)
        ws = _LiveViewerSocket()
        task = asyncio.create_task(viewer_endpoint(ws, "a" * 32, hub, None, lambda: 1_010))
        await _settle(lambda: _frames(s1, FRAME_DATA))          # hello forwarded to t1
        cid = _frames(s1, FRAME_OPEN)[0].channel_id
        t1.end_viewer(cid, CLOSE_KEY_RESOLUTION_REFUSED, "link unavailable")   # what the tunnel loop does
        await _settle(lambda: _frames(s2, FRAME_DATA))          # hello REPLAYED to t2
        assert _frames(s2, FRAME_DATA)[0].payload == b"client-hello"
        assert any(f.channel_id == cid for f in _frames(s1, FRAME_CLOSE))   # t1 told to drop it
        assert ws.close_codes == []                              # viewer still open
        ws.release.set()
        await task
        assert ws.close_codes == [1001]
    asyncio.run(run())


def test_a_dead_first_candidate_is_abandoned_after_the_budget(monkeypatch):
    monkeypatch.setattr(relay_mod, "FAILOVER_OPEN_BUDGET_S", 0.05)
    async def run():
        hub, (s1, t1), (s2, t2) = _two(monkeypatch)
        ws = _LiveViewerSocket()
        task = asyncio.create_task(viewer_endpoint(ws, "b" * 32, hub, None, lambda: 1_010))
        await _settle(lambda: _frames(s2, FRAME_OPEN))           # t1 never answered; t2 opened
        assert _frames(s2, FRAME_DATA)[0].payload == b"client-hello"
        ws.release.set()
        await task
        assert ws.close_codes == [1001]
    asyncio.run(run())


def test_the_budget_does_not_start_until_the_viewer_speaks(monkeypatch):
    """Viewer-first channel: a connector that says nothing before the client
    hello is healthy, not dead."""
    monkeypatch.setattr(relay_mod, "FAILOVER_OPEN_BUDGET_S", 0.05)
    async def run():
        hub, (s1, t1), (s2, t2) = _two(monkeypatch)
        ws = _LiveViewerSocket()
        ws._sent_hello = True                                    # never sends a hello
        task = asyncio.create_task(viewer_endpoint(ws, "c" * 32, hub, None, lambda: 1_010))
        await asyncio.sleep(0.2)
        assert _frames(s1, FRAME_OPEN) and not _frames(s2, FRAME_OPEN)
        ws.release.set()
        await task
    asyncio.run(run())


def test_a_slow_but_healthy_member_is_not_abandoned(monkeypatch):
    monkeypatch.setattr(relay_mod, "FAILOVER_OPEN_BUDGET_S", 0.3)
    async def run():
        hub, (s1, t1), (s2, t2) = _two(monkeypatch)
        ws = _LiveViewerSocket()
        task = asyncio.create_task(viewer_endpoint(ws, "d" * 32, hub, None, lambda: 1_010))
        await _settle(lambda: _frames(s1, FRAME_DATA))
        cid = _frames(s1, FRAME_OPEN)[0].channel_id
        await asyncio.sleep(0.15)
        t1.enqueue_viewer(cid, b"server-hello")                  # served inside the budget
        await asyncio.sleep(0.3)
        assert not _frames(s2, FRAME_OPEN)
        ws.release.set()
        await task
    asyncio.run(run())


def test_every_candidate_refusing_closes_with_the_most_actionable_code(monkeypatch):
    async def run():
        hub, (s1, t1), (s2, t2) = _two(monkeypatch)
        ws = _LiveViewerSocket()
        task = asyncio.create_task(viewer_endpoint(ws, "e" * 32, hub, None, lambda: 1_010))
        await _settle(lambda: _frames(s1, FRAME_DATA))
        t1.end_viewer(_frames(s1, FRAME_OPEN)[0].channel_id, CLOSE_KEY_RESOLUTION_REFUSED, "link unavailable")
        await _settle(lambda: _frames(s2, FRAME_DATA))
        t2.end_viewer(_frames(s2, FRAME_OPEN)[0].channel_id, CLOSE_CONNECTOR_UNARMED, "unarmed")
        await task
        assert ws.close_codes == [CLOSE_CONNECTOR_UNARMED]      # 4501 outranks 4502
        assert "2 member(s) tried" in ws.close_reasons[0]
        assert "aaaaaaaa" in ws.close_reasons[0] and "bbbbbbbb" in ws.close_reasons[0]
    asyncio.run(run())


def test_a_link_level_refusal_is_terminal(monkeypatch):
    """4504 (bad fragment key) is about the link, not the member: no second opinion."""
    async def run():
        hub, (s1, t1), (s2, t2) = _two(monkeypatch)
        ws = _LiveViewerSocket()
        task = asyncio.create_task(viewer_endpoint(ws, "f" * 32, hub, None, lambda: 1_010))
        await _settle(lambda: _frames(s1, FRAME_DATA))
        t1.end_viewer(_frames(s1, FRAME_OPEN)[0].channel_id, CLOSE_VIEWER_HANDSHAKE_FAILED, "bad key")
        await task
        assert ws.close_codes == [CLOSE_VIEWER_HANDSHAKE_FAILED]
        assert not _frames(s2, FRAME_OPEN)
    asyncio.run(run())


def test_a_pinned_link_has_one_candidate_and_no_failover(monkeypatch):
    async def run():
        hub, (s1, t1), (s2, t2) = _two(monkeypatch, serving_machine="aa" * 32)
        ws = _LiveViewerSocket()
        task = asyncio.create_task(viewer_endpoint(ws, "9" * 32, hub, None, lambda: 1_010))
        await _settle(lambda: _frames(s1, FRAME_DATA))
        t1.end_viewer(_frames(s1, FRAME_OPEN)[0].channel_id, CLOSE_KEY_RESOLUTION_REFUSED, "link unavailable")
        await task
        assert ws.close_codes == [CLOSE_KEY_RESOLUTION_REFUSED]
        assert not _frames(s2, FRAME_OPEN)
    asyncio.run(run())


def test_a_refusal_after_the_first_served_byte_closes_the_viewer(monkeypatch):
    async def run():
        hub, (s1, t1), (s2, t2) = _two(monkeypatch)
        ws = _LiveViewerSocket()
        task = asyncio.create_task(viewer_endpoint(ws, "8" * 32, hub, None, lambda: 1_010))
        await _settle(lambda: _frames(s1, FRAME_DATA))
        cid = _frames(s1, FRAME_OPEN)[0].channel_id
        t1.enqueue_viewer(cid, b"server-hello")
        await asyncio.sleep(0.02)
        t1.end_viewer(cid, CLOSE_KEY_RESOLUTION_REFUSED, "late refusal")
        await asyncio.sleep(0.02)
        assert ws.close_codes == [CLOSE_KEY_RESOLUTION_REFUSED]   # closed by the tunnel, not failed over
        ws.release.set()   # a real socket would report the close; the fake must be released
        await task
        assert not _frames(s2, FRAME_OPEN)
    asyncio.run(run())


def test_the_fresh_pin_orders_the_publisher_first_and_fails_over_past_it(monkeypatch):
    async def run():
        hub, (s1, t1), (s2, t2) = _two(monkeypatch)
        t1.channels[b"y" * 16] = object()                          # now t2 is least loaded...
        t1.channels[b"w" * 16] = object()
        token = "7" * 32
        hub.pin_fresh_link(token, "aa" * 32, now=1_000.0)       # ...but aa is the publisher
        ws = _LiveViewerSocket()
        task = asyncio.create_task(viewer_endpoint(ws, token, hub, None, lambda: 1_010))
        await _settle(lambda: _frames(s1, FRAME_DATA))
        assert not _frames(s2, FRAME_OPEN)
        t1.end_viewer(_frames(s1, FRAME_OPEN)[0].channel_id, CLOSE_KEY_RESOLUTION_REFUSED, "x")
        await _settle(lambda: _frames(s2, FRAME_OPEN))
        ws.release.set()
        await task
    asyncio.run(run())
