"""Shared fixtures: an org root and tunnel:serve node identities.

Same key shape as the relaykit suite — every swarm-serving node holds a
``tunnel:serve`` delegation because swarm traffic rides the identical
channel contract (the application seam cannot tell the rungs apart).
"""

from __future__ import annotations

import time

import pytest

from tools.network.idkit import KeyPair, Subject, issue_cert

ORG = "66666666-6666-4666-8666-666666666666"


@pytest.fixture(scope="session")
def now():
    return int(time.time())


@pytest.fixture(scope="session")
def root():
    return KeyPair.generate()


def mint_node(root, now, name):
    """A node keypair + tunnel:serve cert chained to the org root."""
    key = KeyPair.generate()
    cert = issue_cert(
        root,
        key.public_hex,
        scope=("tunnel:serve",),
        org=ORG,
        subject=Subject("agent", name),
        not_before=now - 300,
        not_after=now + 7 * 86_400,
    )
    return key, cert
