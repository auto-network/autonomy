"""Can a SESSION drive a joiner's claim over the relay, or does the joiner
need a browser? (auto-yivxc's transport precondition.)

The live joiner does not use the dashboard's HTTP routes — a container seat is
refused there. They open the published link, which is a viewer channel over
the relay, and the org's connector dispatches `org:join` ops to the same
`claim_service` the HTTP routes call. This test drives exactly that path with
no browser anywhere: a real registry subprocess, the org's real connector
serving `link_serving.make_grant_handler`, and a real `ViewerChannel` sending
`context`, `submit` and `status`.

Stack cloned from test_link_serving_tunnel.py, which proves the same tunnel
for artifact fetches; the difference here is the grant's target_type and the
ops driven over it.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from tools.graph import settings_ops
from tools.graph.db import GraphDB
from tools.graph.schemas.network_identity import (
    NETWORK_LINK_GRANT_REVISION,
    NETWORK_LINK_GRANT_SET_ID,
)
from tools.network.idkit import KeyPair, Subject, issue_cert
from tools.network.idkit.persona import derive_persona
from tools.network.ledger import HLC, LedgerStore, org_ledger_db_path
from tools.network.ledger.events import make_event
from tools.network.ledger.found import found_org_ledger
from tools.network.relaykit.connector import TunnelConnector
from tools.network.relaykit.viewer import ViewerChannel
from tools.dashboard import link_serving

REPO = Path(__file__).resolve().parents[3]
ORG = "join-relay-live"
ORG_UUID = "77777777-7777-4777-8777-777777777788"
ISO = "%Y-%m-%dT%H:%M:%SZ"
OPERATOR_SEED = bytes(reversed(range(32)))
JOINER_SEED = bytes((i * 13 + 9) % 251 for i in range(32))
BEARER = "5c" * 32


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _start_registry(port: int, db: Path, log: Path) -> subprocess.Popen:
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "tools.network.registry",
         "--db", str(db), "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(REPO), env=env, stdout=open(log, "ab"), stderr=subprocess.STDOUT,
    )
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            if httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=1).status_code == 200:
                return proc
        except httpx.HTTPError:
            time.sleep(0.2)
    proc.kill()
    raise RuntimeError(f"registry did not come up; log: {log.read_text()[-2000:]}")


async def _join_op(port: int, token: str, link_pub: str, request: dict,
                   timeout: float = 20.0) -> dict:
    """One join op over a real viewer channel; returns the parsed envelope."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            channel = await ViewerChannel.connect(
                f"ws://127.0.0.1:{port}", token, link_pub=link_pub, org=ORG_UUID,
            )
            break
        except Exception as exc:  # the tunnel may still be dialing
            last = exc
            await asyncio.sleep(0.25)
    else:
        raise AssertionError(f"viewer could not connect within {timeout}s: {last!r}")
    async with channel:
        await channel.send_message(json.dumps(request).encode())
        raw = await channel.recv_message()
    header, _, _ = raw.partition(b"\n")
    return json.loads(header)


@pytest.fixture
def stack(tmp_path, monkeypatch):
    GraphDB.close_all_pooled()
    orgs_dir = tmp_path / "orgs"
    orgs_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    GraphDB.create_org_db(ORG, org_id=ORG_UUID).close()

    root = KeyPair.generate()
    session_key = KeyPair.generate()
    now = int(time.time())
    session_cert = issue_cert(
        root, session_key.public_hex, scope=("tunnel:serve",), org=ORG_UUID,
        subject=Subject("persona", "ab" * 32),
        not_before=now - 300, not_after=now + 86_400,
    )
    port = _free_port()
    registry = _start_registry(port, tmp_path / "registry.db", tmp_path / "registry.log")
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}") as client:
            from tools.network.registry.signing import sign_request
            response = client.post("/v1/orgs", json=sign_request(
                root, "POST", "/v1/orgs",
                {"org_uuid": ORG_UUID, "root_pub": root.public_hex,
                 "recovery_policy": "none"}, ts=now))
            assert response.status_code == 201, response.text
        # Production share links are served under a PER-LINK keypair (the
        # public half rides the URL fragment, the private half is distributed
        # to the org) — graph://807b4e11-3e9. Exercise that, not root anchoring.
        link_key = KeyPair.generate()
        connector = TunnelConnector(
            f"ws://127.0.0.1:{port}", ORG_UUID, session_key, session_cert,
            handler=link_serving.make_grant_handler(ORG),
            link_key_for=lambda _token: link_key,
            min_backoff=0.1, max_backoff=1.0,
        )
        yield {"port": port, "root": root, "root_pub": root.public_hex,
               "link_pub": link_key.public_hex,
               "db": tmp_path / "registry.db", "connector": connector}
    finally:
        registry.terminate()
        registry.wait(timeout=5)
        GraphDB.close_all_pooled()


def _found_with_member_invite(root: KeyPair) -> tuple[str, str, KeyPair]:
    """Found the org, define Member, mint a BEARER Member invitation exactly
    as the Membership screen does. Returns (genesis_id, invite_id, joiner)."""
    import hashlib
    with LedgerStore(org_ledger_db_path(ORG)) as store:
        founded = found_org_ledger(
            store, org_id=ORG_UUID, org_root=root,
            personal_root_seed=OPERATOR_SEED, now=int(time.time() * 1000) - 10_000,
        )
        operator = derive_persona(OPERATOR_SEED, founded.genesis_id)
        base = int(time.time() * 1000)
        store.append(make_event(root, {
            "type": "role.define", "name": "member", "scope_set": [],
            "claim_requires": "admin-ack", "version": 1,
            "approver_threshold": {"kind": "static", "count": 1},
        }, list(store.heads()), HLC(base, 0)))
        invite_id = store.append(make_event(operator, {
            "type": "invite", "granted_role": "member",
            "expiry": base + 7 * 86_400_000, "sponsor": operator.public_hex,
            "token_hash": hashlib.sha256(BEARER.encode()).hexdigest(),
            "max_uses": 25,
        }, list(store.heads()), HLC(base + 1000, 0)))
    return founded.genesis_id, invite_id, derive_persona(JOINER_SEED, founded.genesis_id)


def _cache_join_grant(token: str, invite_ref: str) -> None:
    settings_ops.add_setting(
        NETWORK_LINK_GRANT_SET_ID, NETWORK_LINK_GRANT_REVISION, token,
        {
            "token": token,
            "url": f"https://relay.auto.network/l/{token}",
            "target_uuid": ORG_UUID,
            "target_type": "org:join",
            "invite_ref": invite_ref,
            "meta": {},
            "subject": {"kind": "operator", "id": "op-1"},
            "issued_at": time.strftime(ISO, time.gmtime()),
        },
        org=ORG,
    )


def test_a_session_drives_a_joiner_claim_over_the_relay(stack):
    """THE QUESTION: no browser anywhere — a session opens the published link
    as a viewer channel, under the PER-LINK keypair production uses, and
    drives context + submit + status."""
    from tools.network.registry.testkit import mint_link_at

    genesis_id, invite_id, joiner = _found_with_member_invite(stack["root"])
    token = mint_link_at(stack["db"], ORG_UUID, ORG_UUID)
    _cache_join_grant(token, invite_id)
    GraphDB.close_all_pooled()

    async def run():
        connector = stack["connector"]
        task = asyncio.create_task(connector.run())
        try:
            await asyncio.wait_for(connector.connected.wait(), timeout=15)

            # 1. context — what the joiner's client needs to build a claim.
            context = await _join_op(stack["port"], token, stack["link_pub"],
                                     {"op": "context", "v": 1})
            assert context.get("heads"), context

            # 2. submit — a real member.claim, signed by the joiner persona,
            #    carrying the bearer, sent over the channel.
            claim = make_event(joiner, {
                "type": "member.claim",
                "invite_ref": invite_id,
                "persona_pub": joiner.public_hex,
                "profile": {"display_name": "Relay Joiner"},
                "approvals": [],
                "token": BEARER,
            }, list(context["heads"]), HLC(int(time.time() * 1000) + 5_000, 0))
            submitted = await _join_op(
                stack["port"], token, stack["link_pub"],
                {"op": "submit", "v": 1, "event": claim.to_json().decode()})

            # 3. status — the claim is staged and awaiting the countersignature.
            status = await _join_op(
                stack["port"], token, stack["link_pub"],
                {"op": "status", "v": 1, "persona_pub": joiner.public_hex})
            return context, submitted, status
        finally:
            connector.stop()
            task.cancel()
            with pytest.raises(BaseException):
                await task

    context, submitted, status = asyncio.run(run())

    # The claim reached the org over the relay and is staged for approval:
    # admission needs the operator's countersignature (admin-ack, threshold 1)
    # and then the finalize re-submit — neither of which is transport.
    assert submitted.get("status") == "pending", submitted
    assert status.get("status") == "pending", status
    assert status.get("need") == 1, status
    assert status.get("have") == 0, status

    # And it is really on the org's ledger, staged, not merely acknowledged.
    with LedgerStore(org_ledger_db_path(ORG)) as store:
        staged = [r for r in store.list_pending_claims()
                  if r["persona_pub"] == joiner.public_hex]
        assert staged, "the claim did not reach the org's staging table"
