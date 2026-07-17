"""Shared fixtures for relaykit: a tunnel:serve key hierarchy.

Unit tests (frames, channel crypto) are pure in-memory. The integration
suite (``test_relay_integration.py``) builds its own two-process stack
on top of these keys.
"""

from __future__ import annotations

import time

import pytest

from tools.network.idkit import KeyPair, Subject, issue_cert

ORG = "55555555-5555-4555-8555-555555555555"
TOKEN = "0123456789abcdef0123456789abcdef"

SESSION_SCOPE = ("delegate:agent", "link:publish", "link:revoke", "tunnel:serve")


@pytest.fixture(scope="session")
def now():
    return int(time.time())


@pytest.fixture(scope="session")
def root():
    return KeyPair.generate()


@pytest.fixture(scope="session")
def session_key():
    return KeyPair.generate()


@pytest.fixture(scope="session")
def session_cert(root, session_key, now):
    """root -> session, tunnel:serve included (the §6.3 sign-on shape)."""
    return issue_cert(
        root,
        session_key.public_hex,
        scope=SESSION_SCOPE,
        org=ORG,
        subject=Subject("operator", "op-1"),
        not_before=now - 300,
        not_after=now + 7 * 86_400,
    )
