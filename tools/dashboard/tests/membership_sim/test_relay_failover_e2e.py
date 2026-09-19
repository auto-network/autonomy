"""Viewer failover across a real org pool (graph://d9153c5a-76e O-B, auto-s81lo).

Everything real: a registry subprocess, two authenticated connectors for
one org whose tunnels form its pool, and a real viewer dialing the link
with only its fragment key. The first-registered member has no key for
the link, exactly the member that has not received the grant yet, and
refuses every open with 4502; the second holds it. Before this change the
viewer received the first member's 4502. Now the relay replays the
viewer's hello to the second member and the fetch returns the content.
"""
from __future__ import annotations

import asyncio

import pytest

from tools.network.idkit import KeyPair
from tools.network.relaykit.connector import TunnelConnector

from ._harness import (
    CONTENT, Org, Registry, _proof_resolver, _serve_cert, fetch, run_connector,
)


@pytest.fixture
def registry(tmp_path):
    reg = Registry(tmp_path)
    try:
        yield reg
    finally:
        reg.stop()


def _member(registry, org, persona, *, seq, held: dict):
    """A member dashboard's connector that holds the keys in *held* and,
    like the production resolver, refuses any other token with a
    PermissionError (close 4502) before any byte is served."""
    serve_key, machine_key = KeyPair.generate(), KeyPair.generate()

    def authorization(token):
        key = held.get(token)
        if key is None:
            raise PermissionError("link unavailable")
        return {"protocol": "public-link", "key": key}

    async def handler(token, message):
        return b'{"v": 1, "status": "ok"}\n' + CONTENT

    return TunnelConnector(
        registry.ws, registry.org, serve_key, _serve_cert(persona, serve_key, registry.org),
        handler=handler, machine_key=machine_key, caps=(),
        membership_proof_for=_proof_resolver(org.member_pubs(), persona, seq),
        channel_authorization_for=authorization, min_backoff=0.1, max_backoff=1.0,
    )


def test_a_member_without_the_grant_is_failed_over_to_one_that_has_it(registry, tmp_path):
    org = Org.found()
    registry.register(org.sim.root)
    registry.commit_checkpoint(org.sim, seq=0)
    link_key = KeyPair.generate()
    token = registry.mint_link()

    behind = _member(registry, org, org.founder, seq=0, held={})
    current = _member(registry, org, org.founder, seq=0, held={token: link_key})

    async def run():
        # Register the member WITHOUT the key first: the pool is ordered by
        # registration, so least-loaded tries it first on every dial.
        async with run_connector(behind):
            async with run_connector(current):
                for _ in range(3):
                    assert await fetch(registry, token, link_key.public_hex) == CONTENT

    asyncio.run(run())
    log = (tmp_path / "registry.log").read_text(errors="replace")
    assert "relay viewer failover" in log, log[-2000:]
    assert "4502" in log


def test_every_member_lacking_the_grant_yields_one_honest_refusal(registry, tmp_path):
    org = Org.found()
    registry.register(org.sim.root)
    registry.commit_checkpoint(org.sim, seq=0)
    link_key = KeyPair.generate()
    token = registry.mint_link()

    a = _member(registry, org, org.founder, seq=0, held={})
    b = _member(registry, org, org.founder, seq=0, held={})

    async def run():
        async with run_connector(a):
            async with run_connector(b):
                with pytest.raises(Exception) as excinfo:
                    await fetch(registry, token, link_key.public_hex, timeout=6.0)
                return str(excinfo.value)

    detail = asyncio.run(run())
    log = (tmp_path / "registry.log").read_text(errors="replace")
    assert "failed over every candidate" in log, log[-2000:]
    assert "tried=[" in log and log.count("4502") >= 2, log[-2000:]
