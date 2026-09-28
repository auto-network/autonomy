"""auto-8q7oe.7: viewer relay filtering, origin and authority checks, control handover."""

import asyncio
import struct
import time

import pytest

from tools.dashboard import browser_containers as containers
from tools.dashboard import browser_reconciler as reconciler
from tools.dashboard import browser_viewer as viewer
from tools.dashboard.dao import browser_leases as store
from tools.dashboard.dao import dashboard_db


# ── RFB client message filter ──────────────────────────────────────────

KEY = bytes([4, 1, 0, 0]) + struct.pack(">I", 0x61)             # KeyEvent 'a' down
POINTER = bytes([5, 1]) + struct.pack(">HH", 10, 20)             # PointerEvent
UPDATE = bytes([3, 1]) + struct.pack(">HHHH", 0, 0, 1920, 1080)  # FramebufferUpdateRequest
ENCODINGS = bytes([2, 0]) + struct.pack(">H", 2) + struct.pack(">ii", 0, -223)
CUT = bytes([6, 0, 0, 0]) + struct.pack(">I", 5) + b"hello"
PIXEL_FORMAT = bytes([0]) + bytes(19)


def test_watching_viewers_send_no_input_but_keep_the_screen_updating():
    filt = viewer.ClientFilter()
    stream = ENCODINGS + UPDATE + KEY + POINTER + CUT + UPDATE + PIXEL_FORMAT
    out, saw_input = filt.feed(stream, allow_input=False)
    assert out == ENCODINGS + UPDATE + UPDATE + PIXEL_FORMAT
    assert saw_input is True


def test_the_controlling_viewer_passes_everything():
    filt = viewer.ClientFilter()
    stream = UPDATE + KEY + POINTER + CUT
    assert filt.feed(stream, allow_input=True) == (stream, True)


def test_messages_split_across_websocket_frames_are_reassembled():
    filt = viewer.ClientFilter()
    stream = ENCODINGS + KEY + UPDATE
    collected = b""
    for i in range(len(stream)):
        out, _ = filt.feed(stream[i:i + 1], allow_input=True)
        collected += out
    assert collected == stream and filt.buffer == b""


def test_unknown_messages_and_oversized_clipboard_close_the_connection():
    with pytest.raises(viewer.ProtocolError):
        viewer.ClientFilter().feed(bytes([99, 0, 0, 0]), allow_input=True)
    huge = bytes([6, 0, 0, 0]) + struct.pack(">I", viewer.MAX_CUT_TEXT + 1)
    with pytest.raises(viewer.ProtocolError):
        viewer.ClientFilter().feed(huge, allow_input=True)


def test_vnc_auth_response_is_des_with_bit_reversed_key():
    from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
    from cryptography.hazmat.primitives.ciphers import Cipher, modes

    challenge = bytes(range(16))
    response = viewer.vnc_auth_response("p@ss!wd8extra", challenge)
    key = bytes(int(f"{b:08b}"[::-1], 2) for b in b"p@ss!wd8")  # only 8 characters count
    decrypted = Cipher(TripleDES(key * 3), modes.ECB()).decryptor().update(response)
    assert decrypted == challenge and len(response) == 16


# ── who may open a viewer ──────────────────────────────────────────────


@pytest.mark.parametrize("origin,host,ok", [
    ("https://dash.example:8080", "dash.example:8080", True),
    ("https://evil.example", "dash.example:8080", False),
    ("https://dash.example:8080.evil.example", "dash.example:8080", False),
    ("null", "dash.example:8080", False),
    (None, "dash.example:8080", False),
    ("https://dash.example:8080", None, False),
])
def test_same_origin(origin, host, ok):
    assert viewer.same_origin(origin, host) is ok


def test_find_lease_takes_only_a_16_hex_reference(monkeypatch):
    leases = [type("L", (), {"lease_hash": "abcdef0123456789" + "0" * 48})()]
    monkeypatch.setattr(store, "list_leases", lambda: leases)
    assert viewer.find_lease("abcdef0123456789") is leases[0]
    for bad in ("abcdef012345678", "ABCDEF0123456789", "../../etc/passwd", "brl_" + "0" * 12):
        assert viewer.find_lease(bad) is None


# ── control handover ───────────────────────────────────────────────────


@pytest.fixture
def lease(tmp_path, monkeypatch):
    dashboard_db.init_db(tmp_path / "dashboard.db")
    monkeypatch.setattr(store, "_ready_path", None)
    epoch = store.take_epoch()
    monkeypatch.setattr(reconciler, "_epoch", epoch)
    h = "c" * 64
    store.admit(epoch=epoch, max_leases=4, lease_hash_=h, session="auto-owner", org="autonomy",
                workspace="ws", profile_kind="ephemeral", profile_name=None, adapter="chrome-headed",
                container_name="brw-e-x", expires_at=time.time() + 600, secret="s" * 64,
                vnc_password="pw123456")
    store.transition(h, epoch=epoch, to="starting", address="172.30.0.9")
    store.transition(h, epoch=epoch, to="ready")
    aborts = []
    def agent_request(addr, secret, method, path, body=None, timeout=5):
        aborts.append(path)
        if agent_request.fail:
            return agent_request.fail
        if path == "/login/cleanup":
            return agent_request.cleanup
        return 200, ({"locked": body["locked"]} if path == "/lock" else {})

    agent_request.fail = None
    agent_request.cleanup = (200, {"cleaned": True, "reloaded": False})
    monkeypatch.setattr(containers, "agent_request", agent_request)
    viewer._controls.clear()
    yield h, aborts, agent_request
    viewer._controls.clear()


def test_take_and_return_control(lease):
    h, aborts, _ = lease
    assert viewer.take_control(store.get(h), "viewer-aaaa") == {"state": "locked", "holder": "human"}
    row = store.get(h)
    assert (row.state, row.lock_holder) == ("locked", "human")
    assert viewer.holds_control(h, "viewer-aaaa") and not viewer.holds_control(h, "viewer-bbbb")
    assert aborts == ["/lock"]  # the agent is locked even when idle
    with pytest.raises(viewer.ControlRefused):
        viewer.return_control(store.get(h), "viewer-bbbb")  # only the holder returns it
    assert viewer.return_control(store.get(h), "viewer-aaaa") == {"state": "ready"}
    assert aborts == ["/lock", "/login/cleanup", "/lock"]  # lock; cleanup, then unlock
    assert (store.get(h).state, store.get(h).lock_holder) == ("ready", None)


def test_taking_control_stops_a_running_agent_command(lease):
    h, aborts, _ = lease
    store.transition(h, epoch=reconciler.epoch(), to="busy")
    viewer.take_control(store.get(h), "viewer-aaaa")
    assert aborts == ["/lock"] and store.get(h).state == "locked"  # /lock also stops the command


def test_a_second_viewer_can_take_control_from_the_first(lease):
    h, _, _ = lease
    viewer.take_control(store.get(h), "viewer-aaaa")
    viewer.take_control(store.get(h), "viewer-bbbb")
    assert viewer.holds_control(h, "viewer-bbbb") and not viewer.holds_control(h, "viewer-aaaa")


def test_control_returns_to_the_agent_after_the_holder_leaves(lease, monkeypatch):
    h, _, _ = lease
    monkeypatch.setattr(viewer, "GRACE_S", 0.2)
    viewer.take_control(store.get(h), "viewer-aaaa")

    async def scenario():
        loop = asyncio.get_running_loop()
        viewer.viewer_left(h, "viewer-aaaa", loop)
        await asyncio.sleep(0.05)
        early = store.get(h).state
        await asyncio.sleep(0.6)
        return early, store.get(h).state

    assert asyncio.run(scenario()) == ("locked", "ready")


def test_retaking_control_within_the_grace_keeps_it(lease, monkeypatch):
    h, _, _ = lease
    monkeypatch.setattr(viewer, "GRACE_S", 0.2)
    viewer.take_control(store.get(h), "viewer-aaaa")

    async def scenario():
        loop = asyncio.get_running_loop()
        viewer.viewer_left(h, "viewer-aaaa", loop)
        viewer.take_control(store.get(h), "viewer-aaaa")  # reconnected and took it back
        await asyncio.sleep(0.5)
        return store.get(h).state

    assert asyncio.run(scenario()) == "locked"


def test_no_control_while_the_lease_is_not_running(lease):
    h, _, _ = lease
    store.transition(h, epoch=reconciler.epoch(), to="releasing")
    with pytest.raises(viewer.ControlRefused):
        viewer.take_control(store.get(h), "viewer-aaaa")


# ── the control route: authority and origin ────────────────────────────


def _control_app(monkeypatch, principal):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from tools.dashboard import api_auth, browser_routes

    monkeypatch.setattr(api_auth, "principal_from_request", lambda request: principal)
    monkeypatch.setattr(viewer, "find_lease", lambda ref: None)
    app = Starlette(routes=[r for r in browser_routes.ROUTES if "control" in r.path])
    return TestClient(app, base_url="https://dash.example")


def test_control_refuses_organization_sessions_and_cross_site_origins(monkeypatch):
    from tools.dashboard import unlock_routes
    from tools.dashboard.api_auth import ApiPrincipal, ApiPrincipalKind

    monkeypatch.setattr(unlock_routes, "gate_enforced", lambda: True)
    body = {"action": "take", "viewer": "viewer-aaaa"}
    url = "/api/browser/leases/0123456789abcdef/control"
    own = {"origin": "https://dash.example"}

    org_session = ApiPrincipal(ApiPrincipalKind.ORG_SESSION, subject="auto-x", org="autonomy")
    assert _control_app(monkeypatch, org_session).post(url, json=body, headers=own).status_code == 403

    operator = ApiPrincipal(ApiPrincipalKind.OPERATOR_COOKIE, subject="operator")
    client = _control_app(monkeypatch, operator)
    assert client.post(url, json=body, headers={"origin": "https://evil.example"}).status_code == 403
    assert client.post(url, json=body).status_code == 403            # no Origin at all
    assert client.post(url, json=body, headers=own).status_code == 404  # authorized; no such lease
    assert client.post(url, json={"action": "steal", "viewer": "viewer-aaaa"},
                       headers=own).status_code == 400

    anonymous = ApiPrincipal(ApiPrincipalKind.COMPATIBILITY)
    assert _control_app(monkeypatch, anonymous).post(url, json=body, headers=own).status_code == 401


@pytest.mark.parametrize("failure", [(404, {"error": "not found"}), (0, {}), (200, {"locked": False}),
                                     (401, {"error": "unauthorized"})])
def test_take_control_fails_closed_when_the_agent_does_not_lock(lease, failure):
    h, _, agent_request = lease
    agent_request.fail = failure
    with pytest.raises(viewer.ControlRefused) as refused:
        viewer.take_control(store.get(h), "viewer-aaaa")
    assert refused.value.status == 502
    assert store.get(h).state == "ready" and store.get(h).lock_holder is None
    assert not viewer.holds_control(h, "viewer-aaaa")


def test_take_control_survives_the_aborted_command_freeing_the_lease(lease):
    # Locking the agent stops the running command; its route moves the row
    # busy -> ready before take-control's compare-and-set runs.
    h, _, agent_request = lease
    store.transition(h, epoch=reconciler.epoch(), to="busy")
    snapshot = store.get(h)

    def lock_and_free(addr, secret, method, path, body=None, timeout=5):
        if path == "/lock" and body["locked"]:
            store.transition(h, epoch=reconciler.epoch(), to="ready", expect=("busy",))
        return 200, {"locked": body["locked"]}

    import tools.dashboard.browser_containers as bc
    original = bc.agent_request
    bc.agent_request = lock_and_free
    try:
        assert viewer.take_control(snapshot, "viewer-aaaa") == {"state": "locked", "holder": "human"}
    finally:
        bc.agent_request = original
    assert store.get(h).state == "locked"


@pytest.mark.parametrize("cleanup", [(404, {}), (200, {"cleaned": False}), (500, {})])
def test_control_stays_with_the_operator_until_cleanup_is_confirmed(lease, cleanup):
    h, calls, agent_request = lease
    viewer.take_control(store.get(h), "viewer-aaaa")
    agent_request.cleanup = cleanup
    with pytest.raises(viewer.ControlRefused):
        viewer.return_control(store.get(h), "viewer-aaaa")
    assert (store.get(h).state, store.get(h).lock_holder) == ("locked", "human")
    assert calls.count("/lock") == 1  # the agent was never unlocked


def test_the_pages_are_served_and_load_novnc_from_our_own_origin():
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from tools.dashboard import browser_routes

    app = Starlette(routes=[r for r in browser_routes.ROUTES if getattr(r, "path", "").startswith("/browser")])
    client = TestClient(app)
    for path in ("/browser", "/browser/0123456789abcdef"):
        r = client.get(path)
        assert r.status_code == 200 and "text/html" in r.headers["content-type"]
        assert "/static/vendor/novnc-1.5.0/core/rfb.js" in r.text
        assert "cdn." not in r.text and "https://" not in r.text  # nothing from third-party origins
