"""Case 1 (auto-kxcax): the self-admitting owner claim, end-to-end.

The founder claims the owner role — ``claim_requires: self``, key-bound — so
``claim_requirement_status`` is (0, 0) and no approver is involved. Proven
here past the ledger: the admitted owner is a working serving identity that
authenticates to a real registry by committed membership and serves a
published link to a real viewer through its own tunnel.
"""

from __future__ import annotations

import asyncio

import pytest

from tools.network.idkit import KeyPair

from ._harness import (
    CONTENT, Org, Registry, assert_holds, assert_member, assert_verdict,
    claim_id_of, connect_identity, fetch, identity_admitted, run_connector,
)


@pytest.fixture
def registry(tmp_path):
    reg = Registry(tmp_path)
    try:
        yield reg
    finally:
        reg.stop()


def test_owner_self_admits_and_serves(registry):
    org = Org.found()

    # Ledger: the founder's claim is admitted with nobody countersigning, and
    # the owner role's "*" covers everything — including checkpoint signing.
    state = org.fold()
    assert_verdict(state, claim_id_of(org.sim, org.founder), admitted=True)
    assert_member(state, org.founder, roles=["owner"])
    assert_holds(state, org.founder, "membership:checkpoint")
    assert_holds(state, org.founder, "link:publish")
    assert len(state.members) == 1

    # Registry: the seed commits the founder as sole member and sole
    # checkpointer; the founder's identity authenticates by proof.
    registry.register(org.sim.root)
    registry.commit_checkpoint(org.sim, seq=0)
    assert identity_admitted(registry, org, org.founder, seq=0) is True

    # Wire: the owner serves a link it published; a viewer fetches it
    # byte-exact through the owner's own tunnel.
    link_key = KeyPair.generate()
    token = registry.mint_link()
    conn = connect_identity(registry, org, org.founder, seq=0,
                            link_token=token, link_key=link_key)

    async def run():
        async with run_connector(conn):
            assert await fetch(registry, token, link_key.public_hex) == CONTENT

    asyncio.run(run())
