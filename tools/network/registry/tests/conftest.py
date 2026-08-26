"""Shared fixtures: a registry app on a fake clock + a full key hierarchy.

Every test drives the service through the HTTP surface (FastAPI
TestClient) exactly the way the dashboard will: signed envelopes built
with ``signing.sign_request``. The clock is injected so binding TTLs,
grant expiry, and the I7 purge horizon are all deterministic.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tools.network.idkit import KeyPair, Subject, issue_cert
from tools.network.registry.app import create_app
from tools.network.registry.signing import sign_request

NOW = 1_800_000_000
HOUR = 3600
DAY = 86_400

ORG = "11111111-1111-4111-8111-111111111111"
ORG_NONE = "33333333-3333-4333-8333-333333333333"
TARGET = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

SESSION_SCOPE = ("delegate:agent", "link:publish", "link:revoke", "tunnel:serve", "viewer:identify")


class Clock:
    def __init__(self, start: int = NOW):
        self.now = start

    def __call__(self) -> int:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def app(clock):
    # secure_cookies=False: the TestClient talks plain http to `testserver`
    # and would silently drop Secure session cookies otherwise.
    return create_app(":memory:", now_fn=clock, secure_cookies=False)


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def root():
    return KeyPair.generate()


@pytest.fixture
def recovery():
    return KeyPair.generate()


@pytest.fixture
def session_key():
    return KeyPair.generate()


@pytest.fixture
def agent_key():
    return KeyPair.generate()


@pytest.fixture
def session_cert(root, session_key):
    """Operator sign-on cert: root -> session, broad scope, long window."""
    return issue_cert(
        root,
        session_key.public_hex,
        scope=SESSION_SCOPE,
        org=ORG,
        subject=Subject("operator", "op-1"),
        not_before=NOW - 100,
        not_after=NOW + 300 * DAY,
    )


@pytest.fixture
def agent_cert(root, session_key, session_cert, agent_key):
    """Narrow promptless delegate: session -> agent, link:publish only."""
    return issue_cert(
        session_key,
        agent_key.public_hex,
        scope=("link:publish",),
        org=ORG,
        subject=Subject("agent", "sess-42"),
        not_before=NOW - 50,
        not_after=NOW + 200 * DAY,
        parent_cert=session_cert,
    )


@pytest.fixture
def persona_key():
    return KeyPair.generate()


@pytest.fixture
def persona_cert(session_key, session_cert, persona_key):
    """A persona delegate: session -> persona, identify-only. This is the
    leaf a picker mints an assertion off for 'link this browser as P'."""
    return issue_cert(
        session_key,
        persona_key.public_hex,
        scope=("viewer:identify",),
        org=ORG,
        subject=Subject("persona", "persona-P"),
        not_before=NOW - 50,
        not_after=NOW + 200 * DAY,
        parent_cert=session_cert,
    )


def signed(client, method, path, key, payload, clock, cert=None, expect=None):
    """Send a signed envelope; assert *expect* status when given."""
    if path == "/v1/link-operation-receipts":
        # The public registry input is digest-bound transport context.  It is
        # deliberately outside the envelope retained by Central.
        signed_payload = dict(payload)
        registry_input = signed_payload.pop("registry_input", None)
        request_body = {
            "envelope": sign_request(
                key, method, path, signed_payload, ts=clock.now, cert=cert
            )
        }
        if registry_input is not None:
            request_body["registry_input"] = registry_input
    else:
        envelope = sign_request(
            key, method, path, payload, ts=clock.now, cert=cert
        )
        request_body = envelope
    response = client.request(
        method, path, json=request_body
    )
    if expect is not None:
        assert response.status_code == expect, (response.status_code, response.json())
    return response


def register(client, clock, root, org_uuid=ORG, policy="none", recovery_pub=None, ttl=None):
    payload = {"org_uuid": org_uuid, "root_pub": root.public_hex, "recovery_policy": policy}
    if recovery_pub is not None:
        payload["recovery_pub"] = recovery_pub
    if ttl is not None:
        payload["requested_ttl"] = ttl
    return signed(client, "POST", "/v1/orgs", root, payload, clock)


def publish_link(client, clock, key, cert=None, org=ORG, target=TARGET,
                 target_type="present", meta=None):
    payload = {"org": org, "target_uuid": target, "target_type": target_type}
    if meta is not None:
        payload["meta"] = meta
    return signed(client, "POST", "/v1/links", key, payload, clock, cert=cert)


@pytest.fixture
def bound_org(client, clock, root, recovery):
    """ORG registered with recovery-key policy (the common case)."""
    response = register(client, clock, root, policy="recovery-key",
                        recovery_pub=recovery.public_hex)
    assert response.status_code == 201, response.json()
    return ORG


@pytest.fixture
def bound_org_none(client, clock):
    """A second org bound with recovery policy NONE; returns its root key."""
    org_root = KeyPair.generate()
    response = register(client, clock, org_root, org_uuid=ORG_NONE, policy="none")
    assert response.status_code == 201, response.json()
    return org_root
