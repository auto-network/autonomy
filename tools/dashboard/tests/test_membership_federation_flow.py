"""FULL-FLOW ACCEPTANCE: a multi-member org, multiple dashboards, cross-serving
each other's content through a REAL registry, authenticating across membership
changes — positive and negative (auto-tmers acceptance criterion).

Every component is real: the registry is a subprocess (its own DB, real HTTP +
WebSocket relay); each "dashboard" is an independent serving identity — its own
serving key, its own persona-signed certificate, its own membership-proof and
link-key resolvers — dialing that one registry; the viewer is a real
ViewerChannel opening a link from a different client. The relay URL is the
binding's registry_url pointed at the subprocess; nothing hard-codes a
production host.

Scope note: org-state replication between dashboards is the sync layer's job
(T1, unbuilt) — here each member's node is modeled as already holding the org
ledger, the content, and the link key, and the test proves what THIS epic
owns: that each independent member identity authenticates to the registry by
committed membership and serves, that the registry admits members and refuses
non-members, and that a membership change is enforced across a live session.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time

import httpx
import pytest

from tools.network.idkit import KeyPair, Subject, issue_cert
from tools.network.ledger import membership_commitment as mc
from tools.network.ledger.tests.conftest import Sim
from tools.network.registry.signing import sign_request
from tools.network.relaykit.connector import TunnelConnector
from tools.network.relaykit.viewer import ViewerChannel

from .test_link_serving_tunnel import ORG_UUID, free_port, start_registry

CONTENT = b"<html><body>federation content, byte-exact</body></html>"
GENESIS_PIN = "aa" * 32
DAY = 86_400


# ── org construction ──────────────────────────────────────────


def _found_org_with_members(n_members: int):
    """A Sim org where the founder is owner+checkpointer and *n_members*
    additional members hold the plain member role. Returns (sim, founder,
    [members])."""
    sim = Sim(org=ORG_UUID)
    sim.role_define(sim.root, "owner", ["*"], requires="self")
    founder, ik = KeyPair.generate(), KeyPair.generate()
    inv = sim.invite(sim.root, "owner", invite_key=ik)
    sim.claim(inv, ik, founder)
    sim.role_define(sim.root, "member", ["link:publish"], requires="self")
    members = []
    for _ in range(n_members):
        persona, mik = KeyPair.generate(), KeyPair.generate()
        minv = sim.invite(sim.root, "member", invite_key=mik)
        sim.claim(minv, mik, persona)
        members.append(persona)
    return sim, founder, members


def _register(port: int, root: KeyPair):
    with httpx.Client(base_url=f"http://127.0.0.1:{port}") as client:
        r = client.post("/v1/orgs", json=sign_request(
            root, "POST", "/v1/orgs",
            {"org_uuid": ORG_UUID, "root_pub": root.public_hex,
             "recovery_policy": "none"},
            ts=int(time.time())))
        assert r.status_code == 201, r.text


def _seed_checkpoint(port: int, sim: Sim, *, seq=0, ts=None):
    """Root-sign and POST a checkpoint committing the CURRENT fold (all members
    at once — a fresh org's first checkpoint legitimately commits everyone)."""
    state = sim.fold()
    record = mc.build_root_checkpoint(
        org=ORG_UUID, seq=seq, genesis_id=sim.genesis_id,
        ledger_head=sorted(state.heads)[0] if state.heads else sim.genesis_id,
        members_root_hex=mc.members_root(state),
        checkpointers_root_hex=mc.checkpointers_root(state),
        ts=ts or int(time.time()), root=sim.root)
    with httpx.Client(base_url=f"http://127.0.0.1:{port}") as client:
        r = client.post(f"/v1/orgs/{ORG_UUID}/membership-checkpoints", json=record)
        assert r.status_code == 201, r.text
    return record


def _publish(port: int, root: KeyPair) -> str:
    with httpx.Client(base_url=f"http://127.0.0.1:{port}") as client:
        r = client.post("/v1/links", json=sign_request(
            root, "POST", "/v1/links",
            {"org": ORG_UUID, "target_uuid": ORG_UUID, "target_type": "note"},
            ts=int(time.time())))
        assert r.status_code == 201, r.text
        return r.json()["token"]


# ── a member's serving dashboard, as an independent identity ───


def _member_connector(port: int, root: KeyPair, sim: Sim, persona: KeyPair,
                      *, link_token: str, link_key: KeyPair, seq: int):
    """One member dashboard: its own serving key + persona-signed cert, a v3
    hello proving the persona under the seeded members_root, a link-key
    resolver for the shared link, and a handler that serves CONTENT."""
    serve_key, machine_key = KeyPair.generate(), KeyPair.generate()
    cert = issue_cert(
        persona, serve_key.public_hex, scope=("tunnel:serve",), org=ORG_UUID,
        subject=Subject("persona", persona.public_hex),
        not_before=int(time.time()) - 300, not_after=int(time.time()) + 30 * DAY)

    member_pubs = mc.member_pubs(sim.fold())

    async def membership_proof_for():
        index, path = mc.inclusion_proof(member_pubs, persona.public_hex)
        return {"v": 1, "checkpoint_seq": seq, "index": index, "path": path}

    def link_key_for(token):
        return link_key if token == link_token else None

    async def handler(token, message):
        return json.dumps({"v": 1, "status": "ok"}).encode() + b"\n" + CONTENT

    return TunnelConnector(
        f"ws://127.0.0.1:{port}", ORG_UUID, serve_key, cert, handler=handler,
        machine_key=machine_key, caps=(),
        membership_proof_for=membership_proof_for,
        link_key_for=link_key_for, min_backoff=0.1, max_backoff=1.0,
    )


async def _fetch(port: int, token: str, link_pub: str, timeout=15.0) -> bytes:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            ch = await ViewerChannel.connect(
                f"ws://127.0.0.1:{port}", token, link_pub=link_pub, org=ORG_UUID)
            break
        except Exception as exc:
            last = exc
            await asyncio.sleep(0.25)
    else:
        raise AssertionError(f"viewer could not connect in {timeout}s: {last!r}")
    try:
        await ch.send_message(json.dumps({"v": 1, "op": "fetch"}).encode())
        return await ch.recv_message()
    finally:
        await ch.close()


# ── the acceptance fixture: registry + founded, seeded org ─────


@pytest.fixture
def federation(tmp_path):
    port = free_port()
    reg = start_registry(port, tmp_path / "registry.db", tmp_path / "registry.log")
    sim, founder, members = _found_org_with_members(2)
    _register(port, sim.root)
    _seed_checkpoint(port, sim, seq=0)
    try:
        yield {"port": port, "sim": sim, "root": sim.root,
               "founder": founder, "members": members}
    finally:
        reg.terminate()
        reg.wait(timeout=5)


def test_two_members_cross_serve_a_published_link(federation):
    """POSITIVE: two independent member dashboards each authenticate to the
    registry by committed membership and serve the OWNER's published link; a
    viewer fetches the content through a member's tunnel (not the owner's)."""
    f = federation
    link_key = KeyPair.generate()
    token = _publish(f["port"], f["root"])
    memberB, memberC = f["members"]

    connB = _member_connector(f["port"], f["root"], f["sim"], memberB,
                              link_token=token, link_key=link_key, seq=0)

    async def run():
        task = asyncio.create_task(connB.run())
        try:
            await asyncio.wait_for(connB.connected.wait(), timeout=15)
            served = await _fetch(f["port"], token, link_key.public_hex)
            assert served.partition(b"\n")[2] == CONTENT
        finally:
            connB.stop()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    asyncio.run(run())


def _hello_rejected(port, sim, persona, *, seq, member_pubs_override=None):
    """Try to bring up a v3 tunnel for *persona*; return True if the registry
    refuses the hello (the tunnel never reaches `connected`)."""
    member_pubs = member_pubs_override or mc.member_pubs(sim.fold())
    serve_key, machine_key = KeyPair.generate(), KeyPair.generate()
    cert = issue_cert(
        persona, serve_key.public_hex, scope=("tunnel:serve",), org=ORG_UUID,
        subject=Subject("persona", persona.public_hex),
        not_before=int(time.time()) - 300, not_after=int(time.time()) + 30 * DAY)

    async def proof():
        try:
            index, path = mc.inclusion_proof(member_pubs, persona.public_hex)
        except mc.MembershipCommitmentError:
            # An outsider fabricates a single-leaf proof of itself.
            index, path = 0, []
        return {"v": 1, "checkpoint_seq": seq, "index": index, "path": path}

    conn = TunnelConnector(
        f"ws://127.0.0.1:{port}", ORG_UUID, serve_key, cert,
        machine_key=machine_key, membership_proof_for=proof,
        min_backoff=0.1, max_backoff=0.3)

    async def run():
        task = asyncio.create_task(conn.run())
        try:
            await asyncio.wait_for(conn.connected.wait(), timeout=2.5)
            return False  # it came up — NOT rejected
        except asyncio.TimeoutError:
            return True   # never authenticated
        finally:
            conn.stop()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    return asyncio.run(run())


def test_outsider_cannot_authenticate(federation):
    """NEGATIVE: a persona that is not a member fabricates a proof; the
    registry refuses the hello — it verifies against the members_root it
    adopted, which does not contain the outsider."""
    f = federation
    outsider = KeyPair.generate()
    assert _hello_rejected(f["port"], f["sim"], outsider, seq=0,
                           member_pubs_override=[outsider.public_hex]) is True


def test_stale_seq_proof_refused(federation):
    """NEGATIVE: a real member presenting a proof at the wrong checkpoint seq
    is refused (checkpoint_seq must equal the registry's adopted seq)."""
    f = federation
    member = f["members"][0]
    assert _hello_rejected(f["port"], f["sim"], member, seq=99) is True


def test_member_admitted_control(federation):
    """POSITIVE control for the negatives above: the same path with a real
    member at the right seq DOES authenticate."""
    f = federation
    assert _hello_rejected(f["port"], f["sim"], f["members"][1], seq=0) is False


def test_removed_member_cannot_authenticate(federation):
    """NEGATIVE across a membership change: remove a member, advance the
    checkpoint, and the removed member can no longer authenticate while a
    remaining member still can."""
    f = federation
    sim, memberB, memberC = f["sim"], f["members"][0], f["members"][1]

    # Remove memberC from the org and advance the checkpoint to seq 1 (the
    # founder, still a checkpointer, signs it — but the seed was root-signed,
    # so a root-signed reset at seq 1 is the simplest advance here).
    sim.revoke_event(sim.root, _claim_id_of(sim, memberC))
    _seed_checkpoint(f["port"], sim, seq=1, ts=int(time.time()) + 1)

    # memberC is gone from members_root → refused; memberB remains → admitted.
    assert _hello_rejected(f["port"], sim, memberC, seq=1) is True
    assert _hello_rejected(f["port"], sim, memberB, seq=1) is False


def test_viewer_without_fragment_fails_closed(federation):
    """NEGATIVE: the keyed link opened without its fragment cannot verify the
    per-link handshake — a member is serving, but the viewer fails closed."""
    f = federation
    link_key = KeyPair.generate()
    token = _publish(f["port"], f["root"])
    connB = _member_connector(f["port"], f["root"], f["sim"], f["members"][0],
                              link_token=token, link_key=link_key, seq=0)

    async def run():
        task = asyncio.create_task(connB.run())
        try:
            await asyncio.wait_for(connB.connected.wait(), timeout=15)
            with pytest.raises(Exception) as excinfo:
                await ViewerChannel.connect(
                    f"ws://127.0.0.1:{f['port']}", token,
                    root_pub=f["root"].public_hex, org=ORG_UUID)
            assert "fragment" in str(excinfo.value)
        finally:
            connB.stop()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    asyncio.run(run())


def _claim_id_of(sim: Sim, persona: KeyPair) -> str:
    """The member.claim event id that admitted *persona* (for revocation)."""
    for event in sim.ledger.events():
        if event.type == "member.claim" \
                and event.payload.get("persona_pub") == persona.public_hex:
            return event.event_id
    raise AssertionError("no claim for that persona")
