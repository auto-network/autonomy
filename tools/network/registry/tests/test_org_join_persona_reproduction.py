"""Reproduction of the operator's org:join publish failure (2026-09-05).

The Membership screen signs an org:join invitation, then publishes its public
join route to the registry and gets:

    registry refused the request (403): SignatureError:
    hop 1: signature does not verify against its parent key

This test reproduces it EXACTLY and pins the diagnosis the crypto session's
smoke established: the registry's HTTP publish gate anchors the envelope's
cert chain at the org's BOUND ROOT. When the acting persona IS the bound root
the chain verifies (201); when the acting persona is a DIFFERENT member
persona — which is the autonomy case, where the ledger genesis root differs
from the registered binding root, so the owner persona that signs the invite
is not the bound root — hop 1 fails (403).

The registry is CORRECT here: accepting any persona-signed cert would let
anyone publish an org:join for any org. The resolution is auto-qol1v — route
org:join publish over the org's already-authenticated serving tunnel (the
registry sees only the org, never a persona), retiring the HTTP publish path.
This test is the regression that proves the failure and will flip to the
tunnel path when that lands.
"""

from __future__ import annotations

from tools.network.idkit import KeyPair, Subject, issue_cert

from .conftest import DAY, NOW, ORG, register, signed

ORG_JOIN_INVITE = "ab" * 32
ORG_JOIN_EXPIRY = 1_900_000_000_000


def _org_join_publish(client, clock, session_key, cert):
    return signed(
        client, "POST", "/v1/links", session_key,
        {"org": ORG, "target_uuid": ORG, "target_type": "org:join",
         "invite_ref": ORG_JOIN_INVITE, "expires_at": ORG_JOIN_EXPIRY},
        clock, cert=cert)


def _persona_session_cert(persona: KeyPair, session_key: KeyPair):
    """A session certificate signed by the acting PERSONA (the post-2026-08-12
    shape): persona -> session key, subject carries the persona."""
    return issue_cert(
        persona, session_key.public_hex, scope=("link:publish",), org=ORG,
        subject=Subject("operator", persona.public_hex),
        not_before=NOW - 100, not_after=NOW + 300 * DAY)


def test_persona_equals_bound_root_publishes(client, clock, root):
    """Control: when the acting persona IS the bound root, the chain anchors
    at the persona == root and verifies — 201."""
    register(client, clock, root)  # binding root_pub == root
    session_key = KeyPair.generate()
    cert = _persona_session_cert(root, session_key)  # persona == bound root
    response = _org_join_publish(client, clock, session_key, cert)
    assert response.status_code == 201, response.text


def test_wrong_persona_reproduces_the_operators_403(client, clock, root):
    """The operator's exact failure: the org:join cert is signed by a member
    persona that is NOT the bound root (autonomy's genesis != binding), so the
    registry's root-anchored HTTP gate refuses it at hop 1."""
    register(client, clock, root)  # binding root_pub == root (f084d746 analog)
    owner_persona = KeyPair.generate()  # 3eea6763 analog — NOT the bound root
    session_key = KeyPair.generate()
    cert = _persona_session_cert(owner_persona, session_key)
    response = _org_join_publish(client, clock, session_key, cert)
    assert response.status_code == 403, response.text
    assert "hop 1: signature does not verify against its parent key" \
        in response.json()["detail"]
