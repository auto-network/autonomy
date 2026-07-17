"""End-to-end lifecycle: the flows the registry and dashboard will run.

Everything here uses only the public API surface (``tools.network.idkit``)
— if this file imports from a private module, the library's boundary leaked.
"""

from __future__ import annotations

import pytest

from tools.network.idkit import (
    KeyPair,
    RevocationSet,
    RevokedError,
    Subject,
    generate_token,
    issue_cert,
    issue_revocation,
    verify_chain,
    verify_revocation,
)

from .conftest import DAY, HOUR, NOW, ORG


def test_full_lifecycle_publish_then_revoke():
    # Ceremony: org root is born; operator signs on, minting a session key.
    root = KeyPair.generate()
    session_key = KeyPair.generate()
    session_cert = issue_cert(
        root,
        session_key.public_hex,
        scope=("delegate:agent", "link:publish", "link:revoke", "tunnel:serve", "viewer:identify"),
        org=ORG,
        subject=Subject(kind="operator", id="op-main"),
        not_before=NOW,
        not_after=NOW + DAY,
    )

    # Session launch: a narrow agent key for promptless publishes.
    agent_key = KeyPair.generate()
    agent_cert = issue_cert(
        session_key,
        agent_key.public_hex,
        scope=("link:publish",),
        org=ORG,
        subject=Subject(kind="agent", id="agent-42"),
        not_before=NOW,
        not_after=NOW + HOUR,
        target_types=("present",),
        parent_cert=session_cert,
    )

    # Registry side: verify the wire-form chain against the org binding.
    from tools.network.idkit import DelegationCert

    wire_cert = DelegationCert.from_json(agent_cert.to_json())
    revocations = RevocationSet()
    result = verify_chain(
        wire_cert,
        root.public_hex,
        org=ORG,
        now=NOW + 10,
        revocations=revocations,
        required_scope="link:publish",
        required_target_type="present",
    )
    assert result.subject_id == "agent-42"

    # Grant issuance: an opaque token, unrelated to any target (I2).
    token = generate_token()
    assert len(token) == 32

    # Compromise: the session key is revoked by root; the record travels,
    # verifies, and the agent chain routed through it goes dark.
    record = issue_revocation(
        root,
        session_key.public_hex,
        org=ORG,
        revoked_at=NOW + 20,
        expires_at=NOW + DAY,  # == the session key's natural expiry
        reason="operator sign-out",
    )
    verify_revocation(record, root.public_hex, org=ORG, revoked_cert=session_cert)
    revocations.add(record)

    with pytest.raises(RevokedError):
        verify_chain(
            wire_cert,
            root.public_hex,
            org=ORG,
            now=NOW + 30,
            revocations=revocations,
            required_scope="link:publish",
        )

    # I7: once the revoked key would have expired anyway, the record purges.
    assert revocations.purge_expired(now=NOW + DAY + 1) == 1
    assert len(revocations) == 0
