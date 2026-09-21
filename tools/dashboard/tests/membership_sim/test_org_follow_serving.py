"""End-to-end: a node follows an org's public surface over the real relay
(design of record graph://5f2f5a49-00d §10.1, bead auto-akcr7).

Everything real below the scheduler: a registry subprocess, an authenticated
member connector running the production ``make_grant_handler``, and a real
viewer that dials the ``org:follow`` link with ONLY its fragment key. The
viewer presents no membership and no client credential — the link's fragment
key is the whole authentication (verify_link_server_hello, inside
ViewerChannel.connect). The ``follow`` op is routed to the fleet-sync
scheduler and its reply stream is returned frame by frame.

The scheduler is stubbed so the test's subject is the SERVING wiring — that
the follow op reaches ``scheduler._handle`` with a ``kind="follow"``
admission and that its reply streams back over the channel — not the sync
engine itself.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from tools.dashboard import link_serving
from tools.graph import settings_ops
from tools.graph.db import GraphDB
from tools.graph.schemas.network_identity import (
    NETWORK_LINK_GRANT_REVISION,
    NETWORK_LINK_GRANT_SET_ID,
)
from tools.network.idkit import KeyPair
from tools.network.relaykit.connector import TunnelConnector

from ._harness import Org, Registry, _proof_resolver, _serve_cert, follow, run_connector

ISO = "%Y-%m-%dT%H:%M:%SZ"
GRANT_ORG = "personal"  # the slug scoping this dashboard's own grant cache
PULL = {
    "v": 3, "op": "pull",
    "roster_epoch": "00" * 32, "compat": "11" * 32, "resume": [],
    "scope": "autonomy",
}


class StubScheduler:
    """Records the admission it is called with and streams a two-frame reply
    — the shape a real org-sync sweep/delta has."""

    def __init__(self):
        self.calls = []

    async def _handle(self, token, message, peer_pub, *, admission=None,
                      telemetry_channel=None, **kw):
        self.calls.append(
            {"peer_pub": peer_pub, "admission": admission, "message": message}
        )

        async def _stream():
            yield b"public-sweep-frame-1"
            yield b"public-sweep-frame-2"

        return _stream()


class StubRuntime:
    def __init__(self, scheduler):
        self.scheduler = scheduler


@pytest.fixture
def registry(tmp_path):
    reg = Registry(tmp_path)
    try:
        yield reg
    finally:
        reg.stop()


@pytest.fixture
def grant_cache(tmp_path, monkeypatch):
    """A per-test grant cache for GRANT_ORG, exactly as the stack fixture
    establishes one: an isolated orgs tree, no GRAPH_DB pin, a materialized
    org DB on disk (the refuse-real-data autouse would otherwise raise)."""
    GraphDB.close_all_pooled()
    orgs_dir = tmp_path / "orgs"
    orgs_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    GraphDB.create_org_db(GRANT_ORG).close()
    yield
    GraphDB.close_all_pooled()


def _cache_follow_grant(token: str, org_uuid: str) -> None:
    settings_ops.add_setting(
        NETWORK_LINK_GRANT_SET_ID, NETWORK_LINK_GRANT_REVISION, token,
        {
            "token": token,
            "url": f"https://relay.auto.network/l/{token}",
            "target_uuid": org_uuid,
            "target_type": "org:follow",
            "meta": {"org": "autonomy", "org_uuid": org_uuid},
            "subject": {"kind": "operator", "id": "op-1"},
            "issued_at": time.strftime(ISO, time.gmtime()),
        },
        org=GRANT_ORG,
    )


def _serving_member(registry, org, persona, *, seq, token, link_key, scheduler):
    """A member dashboard serving with the production grant handler, armed
    with a (stub) fleet runtime so the follow op resolves the scheduler."""
    serve_key, machine_key = KeyPair.generate(), KeyPair.generate()

    return TunnelConnector(
        registry.ws, registry.org, serve_key,
        _serve_cert(persona, serve_key, registry.org),
        handler=link_serving.make_grant_handler(
            GRANT_ORG, fleet_runtime=StubRuntime(scheduler),
        ),
        machine_key=machine_key, caps=(),
        membership_proof_for=_proof_resolver(org.member_pubs(), persona, seq),
        channel_authorization_for=lambda t: {
            "protocol": "public-link",
            "key": link_key if t == token else None,
        },
        min_backoff=0.1, max_backoff=1.0,
    )


GENESIS = "7e" * 32  # the org's ledger genesis id: the follow admission's org


def test_follow_link_opens_with_fragment_key_and_streams_scheduler_reply(
    registry, grant_cache, monkeypatch,
):
    # The link server names the org by its ledger genesis id, resolved from
    # the grant's org slug (auto-8cpnm); this harness org has no ledger file.
    monkeypatch.setattr(link_serving, "_follow_genesis_id", lambda slug: GENESIS)
    org = Org.found()
    registry.register(org.sim.root)
    registry.commit_checkpoint(org.sim, seq=0)

    link_key = KeyPair.generate()
    token = registry.mint_link(target_type="org:follow")
    _cache_follow_grant(token, registry.org)

    scheduler = StubScheduler()
    member = _serving_member(
        registry, org, org.founder, seq=0,
        token=token, link_key=link_key, scheduler=scheduler,
    )

    async def run():
        async with run_connector(member):
            return await follow(registry, token, link_key.public_hex, PULL)

    frames = asyncio.run(run())

    assert frames == [b"public-sweep-frame-1", b"public-sweep-frame-2"]
    assert len(scheduler.calls) == 1
    call = scheduler.calls[0]
    # No client credential; a follow admission built from the grant.
    assert call["peer_pub"] == ""
    assert call["admission"].kind == "follow"
    assert call["admission"].org == GENESIS
