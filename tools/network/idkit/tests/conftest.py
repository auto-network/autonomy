"""Shared fixtures: a canonical root -> session -> agent delegation chain.

All timestamps are fixed integers so tests are deterministic; ``NOW`` sits
inside every fixture cert's validity window.

``force_sign`` builds certificates that bypass issuance-time validation —
adversarial tests need chains that a well-behaved issuer would refuse to
mint (scope escalation, window violations, wrong org) but that carry
*valid signatures*, so verification is what must catch them.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

import pytest

from tools.network.idkit import DelegationCert, KeyPair, Subject, issue_cert

NOW = 1_800_000_000
HOUR = 3600
DAY = 86_400

ORG = "11111111-1111-4111-8111-111111111111"
OTHER_ORG = "22222222-2222-4222-8222-222222222222"

SESSION_SCOPE = ("delegate:agent", "link:publish", "link:revoke", "tunnel:serve", "viewer:identify")
AGENT_SCOPE = ("link:publish",)

SESSION_WINDOW = (NOW - HOUR, NOW + DAY)
AGENT_WINDOW = (NOW - HOUR // 2, NOW + HOUR)


def force_sign(
    signer: KeyPair,
    *,
    child_pub: str,
    scope,
    org: str,
    subject: Subject,
    not_before: int,
    not_after: int,
    target_types=None,
    parent_cert=None,
) -> DelegationCert:
    """Sign an arbitrary cert payload, skipping issue-time narrowing checks."""
    unsigned = DelegationCert(
        child_pub=child_pub,
        scope=tuple(scope),
        org=org,
        subject=subject,
        not_before=not_before,
        not_after=not_after,
        sig="0" * 128,
        target_types=tuple(target_types) if target_types is not None else None,
        parent_cert=parent_cert,
    )
    return dataclasses.replace(unsigned, sig=signer.sign_hex(unsigned.signing_input()))


@dataclass
class Chain:
    root: KeyPair
    session_key: KeyPair
    session_cert: DelegationCert
    agent_key: KeyPair
    agent_cert: DelegationCert

    @property
    def root_pub(self) -> str:
        return self.root.public_hex


def build_chain(org: str = ORG) -> Chain:
    root = KeyPair.generate()
    session_key = KeyPair.generate()
    session_cert = issue_cert(
        root,
        session_key.public_hex,
        scope=SESSION_SCOPE,
        org=org,
        subject=Subject(kind="operator", id="operator-session-1"),
        not_before=SESSION_WINDOW[0],
        not_after=SESSION_WINDOW[1],
    )
    agent_key = KeyPair.generate()
    agent_cert = issue_cert(
        session_key,
        agent_key.public_hex,
        scope=AGENT_SCOPE,
        org=org,
        subject=Subject(kind="agent", id="agent-session-1"),
        not_before=AGENT_WINDOW[0],
        not_after=AGENT_WINDOW[1],
        parent_cert=session_cert,
    )
    return Chain(
        root=root,
        session_key=session_key,
        session_cert=session_cert,
        agent_key=agent_key,
        agent_cert=agent_cert,
    )


@pytest.fixture
def chain() -> Chain:
    return build_chain()
