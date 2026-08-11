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
import json
import os
import socket
import subprocess
import sys
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
    connector = subprocess.Popen(
        [sys.executable, "-m", "tools.dashboard.link_serving",
         "--relay", f"ws://127.0.0.1:{registry_port}", "--org", ORG_UUID,
         "--key-file", str(key_file), "--cert-file", str(cert_file),
         "--graph-org", GRAPH_ORG,
         "--min-backoff", "0.1", "--max-backoff", "1.0"],
        cwd=str(REPO), env=env,
        stdout=open(tmp / "connector.log", "ab"), stderr=subprocess.STDOUT,
    )

    state = {
        "tmp": tmp, "token": token, "registry_port": registry_port,
        "root_pub": root.public_hex, "mission_db": mission_db,
        "guest": guest, "pillar": pillar,
        "procs": {"registry": registry, "connector": connector},
    }
    yield state
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
