"""A named guest using a mission over a REAL relay, end to end (auto-nej35).

Topology -- three real processes, no stubs on the path:

    viewer (this test, real ViewerChannel + real X25519 handshake)
       │ ws
       ▼
    registry + relay   (subprocess: python -m tools.network.registry)
       ▲ ws
       │
    connector          (subprocess: python -m tools.dashboard.link_serving)
       │
       └── the real grant cache, the real mission DB

What every other test in this epic stubs, this one runs for real: the
channel crypto, the relay's own routing, the grant gate, and the read and
write ops. The one thing it cannot cover is the public internet and a
device with no tailnet access -- that is the operator's own final check.

The guest here is NAMED: the grant carries meta.participant_id, and the
point of the write assertions is that the question lands attributed to
that bound identity without the guest ever supplying it.
"""

from __future__ import annotations

import asyncio
import contextlib
import http.server
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

from tools.network.idkit import KeyPair
from tools.network.registry.signing import sign_request
from tools.network.relaykit.viewer import ViewerChannel

REPO = Path(__file__).resolve().parents[3]
ORG_UUID = "66666666-6666-4666-8666-666666666666"
GRAPH_ORG = "acceptorg"

def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class _FakeDashboardEvents:
    """Stand-in for the dashboard's ``GET /api/events`` SSE endpoint.

    The connector subprocess is the real publisher: it subscribes to this
    stream, and an event pushed here travels the WHOLE production path --
    ``relay_publisher.publish_event`` -> real seal -> real ``Publisher``
    -> real tunnel -> real registry fan-out -> the guest's WebSocket.

    That path is the point. Every other test in this epic stubs some part
    of it, which is how a feature that cannot deliver a single frame in
    production shipped with 74 passing tests.

    HTTP/1.0 deliberately: an SSE body has no Content-Length, so the
    stream is framed by connection close, which ``BaseHTTPRequestHandler``
    gives us for free without implementing chunked encoding.
    """

    def __init__(self) -> None:
        self._queue: queue.Queue = queue.Queue()
        self.port = free_port()
        self._server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", self.port), self._handler()
        )
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def _handler(self):
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_GET(self):  # noqa: N802 - stdlib naming
                if self.path != "/api/events":
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                while True:
                    try:
                        topic, data = outer._queue.get(timeout=0.5)
                        frame = f"event: {topic}\ndata: {json.dumps(data)}\n\n"
                    except queue.Empty:
                        frame = ": keepalive\n\n"   # also detects a dead peer
                    try:
                        self.wfile.write(frame.encode("utf-8"))
                        self.wfile.flush()
                    except Exception:
                        return

            def log_message(self, *_args):
                pass

        return Handler

    def emit(self, topic: str, data: dict) -> None:
        self._queue.put((topic, data))

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._server.shutdown()
        with contextlib.suppress(Exception):
            self._server.server_close()


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    """Registry + connector as real subprocesses, wired to real stores."""
    from tools.network.idkit import Subject, issue_cert

    tmp = tmp_path_factory.mktemp("mission-relay")
    graph_db = tmp / "graph.db"
    mission_db = tmp / "mission_control.db"
    registry_db = tmp / "registry.db"
    registry_port = free_port()

    env = {
        **os.environ,
        "PYTHONPATH": str(REPO),
        "GRAPH_DB": str(graph_db),
        "GRAPH_ORG": GRAPH_ORG,
        "MISSION_CONTROL_DB": str(mission_db),
    }

    # -- the org's own key material (the C2/C5 ceremony's output) --------
    root = KeyPair.generate()
    session_key = KeyPair.generate()
    now = int(time.time())
    session_cert = issue_cert(
        root, session_key.public_hex,
        scope=("delegate:agent", "link:publish", "link:revoke", "tunnel:serve"),
        org=ORG_UUID, subject=Subject("operator", "op-1"),
        not_before=now - 300, not_after=now + 7 * 86_400,
    )

    # -- registry subprocess ---------------------------------------------
    registry = subprocess.Popen(
        [sys.executable, "-m", "tools.network.registry",
         "--db", str(registry_db), "--host", "127.0.0.1", "--port", str(registry_port)],
        cwd=str(REPO), env=env,
        stdout=open(tmp / "registry.log", "ab"), stderr=subprocess.STDOUT,
    )
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            if httpx.get(f"http://127.0.0.1:{registry_port}/healthz", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.2)
    else:
        registry.kill()
        pytest.fail(f"registry never came up:\n{(tmp / 'registry.log').read_text()[-2000:]}")

    # -- register the org and publish a mission link ----------------------
    with httpx.Client(base_url=f"http://127.0.0.1:{registry_port}") as client:
        ts = int(time.time())
        r = client.post("/v1/orgs", json=sign_request(
            root, "POST", "/v1/orgs",
            {"org_uuid": ORG_UUID, "root_pub": root.public_hex, "recovery_policy": "none"},
            ts=ts,
        ))
        assert r.status_code == 201, r.text
    # -- the dashboard's own state: mission, pillar, guest, grant ---------
    # Written in-process, read by the connector subprocess -- so THIS
    # process must point at the same stores, not the ambient ones, or the
    # grant lands in the operator's real DB and the connector (correctly)
    # refuses a token it cannot find.
    prior_env = {k: os.environ.get(k) for k in ("GRAPH_DB", "GRAPH_ORG", "MISSION_CONTROL_DB")}
    os.environ["GRAPH_DB"] = str(graph_db)
    os.environ["GRAPH_ORG"] = GRAPH_ORG
    os.environ["MISSION_CONTROL_DB"] = str(mission_db)

    from tools.dashboard.dao import mission_control_db as mcdb
    from tools.graph import settings_ops
    from tools.graph.db import GraphDB

    GraphDB.close_all_pooled()
    from tools.graph.schemas.network_identity import (
        NETWORK_LINK_GRANT_REVISION, NETWORK_LINK_GRANT_SET_ID,
    )

    mcdb.DB_PATH = mission_db
    mcdb.init_db(mission_db)
    # create_mission mints its own id, so the LINK must be published
    # against that id rather than a constant chosen up front.
    mission = mcdb.create_mission("OSS Insights", "auto-coordinator", db_path=mission_db)
    mission_id = mission["mission_id"]
    mcdb.push_site_revision(
        mission_id, "<html><body><h1>OSS Insights</h1></body></html>",
        db_path=mission_db,
    )
    pillar = mcdb.create_pillar(
        mission_id, "Dataset & Schema", "auto-pillar", "#34d399", db_path=mission_db,
    )
    mcdb.push_pillar_site_revision(
        pillar["pillar_id"], "<html><body>pillar detail</body></html>", db_path=mission_db,
    )
    guest = mcdb.create_visitor_token("Priya (data partner)", db_path=mission_db)

    # Publish the link at the registry now that the mission id exists.
    with httpx.Client(base_url=f"http://127.0.0.1:{registry_port}") as client:
        r = client.post("/v1/links", json=sign_request(
            root, "POST", "/v1/links",
            {"org": ORG_UUID, "target_uuid": mission_id, "target_type": "mission"},
            ts=int(time.time()),
        ))
        assert r.status_code == 201, r.text
        token = r.json()["token"]

    settings_ops.add_setting(
        NETWORK_LINK_GRANT_SET_ID, NETWORK_LINK_GRANT_REVISION, token,
        {
            "token": token,
            # The schema requires the canonical HTTPS /l/<token> form. It is
            # descriptive metadata only -- the channel is opened against the
            # local registry below, not against this URL.
            "url": f"https://relay.auto.network/l/{token}",
            "target_uuid": mission_id,
            "target_type": "mission",
            # The whole point: this link is bound to ONE named guest.
            "meta": {"participant_id": guest["participant_id"], "label": "Briefing"},
            "subject": {"kind": "operator", "id": "op-1"},
            "issued_at": _iso(time.time()),
        },
        org=GRAPH_ORG,
    )

    # -- connector subprocess: the REAL grant-gated serving handler -------
    key_file, cert_file = tmp / "session.hex", tmp / "session.cert"
    key_file.write_text(session_key.private_hex)
    cert_file.write_text(session_cert.to_json().decode("ascii"))
    # The connector's publish loop needs an event source. Without an
    # explicit --dashboard-url it falls back to _default_dashboard_url(),
    # which in a dev container resolves to the OPERATOR'S REAL DASHBOARD
    # on localhost:8080 -- so the test connector would subscribe to live
    # production events. Point it at our own stream instead.
    events = _FakeDashboardEvents()
    connector = subprocess.Popen(
        [sys.executable, "-m", "tools.dashboard.link_serving",
         "--relay", f"ws://127.0.0.1:{registry_port}", "--org", ORG_UUID,
         "--key-file", str(key_file), "--cert-file", str(cert_file),
         "--graph-org", GRAPH_ORG,
         "--dashboard-url", f"http://127.0.0.1:{events.port}",
         "--min-backoff", "0.1", "--max-backoff", "1.0"],
        cwd=str(REPO), env=env,
        stdout=open(tmp / "connector.log", "ab"), stderr=subprocess.STDOUT,
    )

    state = {
        "tmp": tmp, "token": token, "registry_port": registry_port,
        "root_pub": root.public_hex, "mission_db": mission_db,
        "guest": guest, "pillar": pillar, "mission_id": mission_id,
        "events": events,
        "procs": {"registry": registry, "connector": connector},
    }
    yield state
    events.close()
    for proc in state["procs"].values():
        with contextlib.suppress(Exception):
            proc.terminate()
    for proc in state["procs"].values():
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)
    GraphDB.close_all_pooled()
    for key, value in prior_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


async def open_channel(state, timeout=30.0) -> ViewerChannel:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            return await ViewerChannel.connect(
                f"ws://127.0.0.1:{state['registry_port']}", state["token"],
                root_pub=state["root_pub"], org=ORG_UUID,
            )
        except Exception as exc:
            last = exc
            await asyncio.sleep(0.3)
    log = (state["tmp"] / "connector.log").read_text()[-2000:]
    raise AssertionError(f"viewer could not connect: {last!r}\nconnector log:\n{log}")


async def op(channel, request: dict) -> dict:
    await channel.send_message(json.dumps(request).encode())
    raw = await channel.recv_message()
    return json.loads(raw.split(b"\n", 1)[0])


def run(state, coro_factory):
    async def main():
        async with await open_channel(state) as channel:
            return await coro_factory(channel)
    return asyncio.run(main())


# ── the artifact still serves ───────────────────────────────────────────


def test_the_mission_renders_over_the_channel(stack):
    header = run(stack, lambda ch: op(ch, {"v": 1, "op": "head"}))
    assert header["status"] == "ok"
    assert header["serialized_size"] > 0


# ── reads: what the shim's fetch() interception becomes ─────────────────


def test_guest_reads_the_pillar_list(stack):
    body = run(stack, lambda ch: op(ch, {"v": 1, "op": "read", "body": {"kind": "pillars"}}))
    assert body["status"] == "ok"
    assert [p["name"] for p in body["pillars"]] == ["Dataset & Schema"]


def test_guest_reads_a_pillars_own_page(stack):
    pillar_id = stack["pillar"]["pillar_id"]
    body = run(stack, lambda ch: op(ch, {
        "v": 1, "op": "read", "body": {"kind": "pillar_site", "pillar_id": pillar_id},
    }))
    assert body["status"] == "ok"
    assert "pillar detail" in body["html"]


def test_guest_reads_presence(stack):
    body = run(stack, lambda ch: op(ch, {"v": 1, "op": "read", "body": {"kind": "presence"}}))
    assert body["status"] == "ok"
    assert isinstance(body["presence"], list)


# ── writes: attributed to the grant's bound guest, never a claim ────────


def test_guest_asks_a_question_and_it_is_attributed_to_the_bound_identity(stack):
    body = run(stack, lambda ch: op(ch, {
        "v": 1, "op": "write",
        "body": {"kind": "question", "question": "Which datasets are in scope?"},
    }))
    assert body["status"] == "ok"
    entry = body["question"]
    assert entry["question"] == "Which datasets are in scope?"
    # The guest never sent an identity -- the channel's own grant did.
    assert entry["asked_by_participant_id"] == stack["guest"]["participant_id"]
    assert entry["asked_by_label"] == "Priya (data partner)"


def test_the_question_is_then_readable_over_the_same_channel(stack):
    body = run(stack, lambda ch: op(ch, {"v": 1, "op": "read", "body": {"kind": "questions"}}))
    assert body["status"] == "ok"
    assert any(
        q["question"] == "Which datasets are in scope?" for q in body["questions"]
    )


def test_a_write_cannot_claim_another_identity(stack):
    """The wire protocol gives the client no identity field, so an attempt
    to smuggle one is simply ignored -- the bound guest is used."""
    body = run(stack, lambda ch: op(ch, {
        "v": 1, "op": "write",
        "body": {
            "kind": "question", "question": "smuggle attempt",
            "asked_by_participant_id": "guest:someone-else",
            "participant_id": "guest:someone-else",
        },
    }))
    assert body in ({"v": 1, "status": "unavailable"},) or body["status"] == "ok"
    if body.get("status") == "ok":
        assert body["question"]["asked_by_participant_id"] == stack["guest"]["participant_id"]


# ── the stream key a live-update subscriber receives ────────────────────


def test_guest_subscribes_and_receives_a_stream_key(stack):
    body = run(stack, lambda ch: op(ch, {"v": 1, "op": "subscribe"}))
    assert body["status"] == "ok"
    assert len(bytes.fromhex(body["stream_key"])) == 32


# ── the negative that matters ───────────────────────────────────────────


def test_a_pillar_of_another_mission_is_refused(stack):
    raw_result = run(stack, lambda ch: op(ch, {
        "v": 1, "op": "read",
        "body": {"kind": "pillar_site", "pillar_id": "99999999-9999-4999-8999-999999999999"},
    }))
    assert raw_result.get("status") != "ok"




# ── stream-key crypto and provenance (NOT delivery) ─────────────────────


def test_a_sealed_frame_opens_only_with_the_guests_own_stream_key(stack):
    """Stream-key crypto and provenance: the subscribe op hands out this
    link's key over the real channel, a frame sealed with that key opens,
    and a frame sealed with any other key does not.

    SCOPE, HONESTLY -- this test used to claim to be "THE FULL LOOP" and
    "over the real channel", and it is neither. It binds
    ``Publisher.send_frame`` to a Python list and opens ``sent[0]``
    directly, so the frame never touches the tunnel, the registry's
    fan-out, the guest's WebSocket, or the record decoder. That mislabel
    is why a feature which cannot deliver a single frame in production sat
    behind a green suite. Delivery is covered by the two transport tests
    at the end of this file; this one covers the crypto beneath it.

    IT ALSO PINS AN INVARIANT NOTHING ELSE ENFORCED: _STREAM_KEYS is an
    in-memory, per-process dict, so whoever seals a frame MUST be the same
    process that answered the guest's subscribe. Production holds by
    construction -- link_serving.main() starts the publish loop inside the
    connector -- but a later refactor moving the publisher into its own
    process would emit frames no guest could open, with no error anywhere.
    The first assertion below is that failure, reproduced on purpose.
    """
    from tools.dashboard import link_serving
    from tools.dashboard.plugins.mission_control import relay_publisher
    from tools.network.relaykit.channel import open_stream_frame, seal_stream_frame
    from tools.network.relaykit.connector import Publisher

    async def run():
        async with await open_channel(stack) as channel:
            envelope = await op(channel, {"v": 1, "op": "subscribe"})
            assert envelope["status"] == "ok"
            guest_key = bytes.fromhex(envelope["stream_key"])
            assert len(guest_key) == 32

            # A key minted in a DIFFERENT process is a different key, and a
            # frame sealed with it is unreadable to this guest.
            other_process_key = link_serving._stream_key(stack["token"])
            assert other_process_key != guest_key
            assert open_stream_frame(
                guest_key, seal_stream_frame(other_process_key, b"{}")
            ) is None

            # The real path: seal with the key that was handed out.
            publisher = Publisher()
            sent = []

            async def send_frame(frame_type, channel_id, payload=b""):
                sent.append(payload)

            publisher.bind(send_frame)
            publisher.attached(stack["token"])
            link_serving._STREAM_KEYS[stack["token"]] = guest_key

            count = await relay_publisher.publish_event(
                publisher,
                relay_publisher.CONVERSATION_TOPIC,
                {
                    "event": "answered",
                    "mission_id": stack["mission_id"],
                    "pillar_id": None,
                    "entry_id": "e-live",
                    "question": {"entry_id": "e-live", "answer": "yes, live"},
                    "update": None,
                },
                org=GRAPH_ORG,
            )
            assert count == 1, "the answer produced no frame"

            opened = open_stream_frame(guest_key, sent[0])
            assert opened is not None, "the guest's key did not open the frame"
            body = json.loads(opened)
            assert body["kind"] == "conversation"
            assert body["event"] == "answered"
            assert body["question"]["answer"] == "yes, live"

            # No other link's key ever opens it.
            assert open_stream_frame(bytes(32), sent[0]) is None

    asyncio.run(run())


# ── live updates, through the ACTUAL transport (auto-8npih regression) ───
#
# The test above is not the end-to-end test its docstring claims. It binds
# Publisher.send_frame to a Python list and opens list[0] directly, so the
# frame never touches the tunnel, the registry's fan-out, the guest's
# WebSocket, or ViewerChannel's record decoder. It proves the sealing
# crypto and key provenance -- worth keeping, now honestly labelled -- and
# nothing about delivery.
#
# The two tests below cross that boundary. Both used to fail, for the
# same root cause -- now fixed by a one-byte kind tag on every
# registry -> viewer message (frames.VIEWER_KIND_RECORD / _FEED):
#
#   relay.py:262   fans the raw sealed frame onto the SAME viewer socket
#                  that carries pairwise channel records, with no tag
#                  distinguishing the two.
#   channel.py:391 ViewerChannel (and SecureChannel.recvRecord in
#                  autonet.js, identically) parses every binary message as
#                  [8-byte seq][ciphertext] and raises "record out of
#                  sequence" on mismatch, poisoning the channel.
#
# A stream frame opens with a 12-byte random nonce, so the seq check
# essentially never passed, and live push never delivered a frame to a
# browser. The tag makes the two kinds distinguishable before either
# decoder sees them; nothing guesses, and the relay still cannot read
# either payload.


def _emit_answer(stack, entry_id="e-transport"):
    stack["events"].emit("mission_control:conversation", {
        "event": "answered",
        "mission_id": stack["mission_id"],
        "pillar_id": None,
        "entry_id": entry_id,
        "question": {"entry_id": entry_id, "answer": "yes, over the wire"},
        "update": None,
    })


def test_a_published_answer_reaches_the_guest_over_the_real_socket(stack):
    """THE loop the epic claimed: dashboard event -> connector publish ->
    tunnel -> registry fan-out -> guest WebSocket -> guest decodes it.

    Nothing on this path is stubbed. The only test double is the event
    SOURCE (an SSE endpoint standing in for the dashboard), because the
    dashboard itself is not what is under test here.
    """
    from tools.network.relaykit.channel import open_stream_frame

    async def main():
        async with await open_channel(stack) as channel:
            envelope = await op(channel, {"v": 1, "op": "subscribe"})
            assert envelope["status"] == "ok"
            guest_key = bytes.fromhex(envelope["stream_key"])

            _emit_answer(stack)

            # The guest is a browser: ONE socket, two kinds of message.
            # The leading kind byte is what lets the reader route a feed
            # frame to the stream decoder instead of handing it to the
            # pairwise decoder, which used to kill the channel.
            raw = await asyncio.wait_for(channel.recv_feed(), timeout=15)

            opened = open_stream_frame(guest_key, raw)
            assert opened is not None, "guest could not open the pushed frame"
            body = json.loads(opened)
            assert body["kind"] == "conversation"
            assert body["question"]["answer"] == "yes, over the wire"

            # And the channel must still work afterwards: a feed frame is
            # an interleaved event, not the end of the conversation.
            header = await op(channel, {"v": 1, "op": "head"})
            assert header["status"] == "ok"

    asyncio.run(main())


def test_a_frame_published_during_startup_does_not_break_the_first_fetch(stack):
    """The worse failure mode, and the likely cause of the reported
    'pillar dropdown is empty' bug.

    A guest opening a link is registered as a stream listener the instant
    the socket opens. If a coordinator answers a question in that window,
    the frame lands mid-fetch, fails the sequence check, and closes the
    channel -- so the page renders nothing at all, top bar stuck on
    'Loading...'. No subscribe is issued here precisely because the race
    does not require one.
    """
    async def main():
        async with await open_channel(stack) as channel:
            _emit_answer(stack, entry_id="e-race")
            await asyncio.sleep(0.4)          # let the frame arrive first

            header = await asyncio.wait_for(
                op(channel, {"v": 1, "op": "head"}), timeout=15
            )
            assert header["status"] == "ok", "the artifact fetch was poisoned"

    asyncio.run(main())
