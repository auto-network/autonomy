"""Throwaway test-identity and test-org factory (§21 prerequisite).

The governing build principle (crib §21): the PRIMARY acceptance of every vault
bead is the full protocol driven HEADLESS with throwaway identities and test
orgs — no browser, no human-entered password or PRF. That is impossible without
a factory that mints a disposable factor and a disposable genesis. This is it.

Everything here is deterministic-free but self-contained: a test password is a
literal, a genesis is a random hex id. No network, no real org, no operator.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from .factors import (
    PasswordFactor,
    PublishedFactor,
    create_passkey_factor,
    create_password_factor,
    random_seed,
)


@dataclass(frozen=True)
class TestIdentity:
    """A throwaway password factor plus the password that opens it.

    In a real system the password is typed by a human and never stored; here it
    is carried in the clear precisely because the identity is disposable and the
    whole point is to exercise the protocol with no human present.
    """

    factor_id: str
    password: str
    factor: PasswordFactor  # .published, .armor

    @property
    def published(self) -> PublishedFactor:
        return self.factor.published

    @property
    def armor(self) -> str:
        return self.factor.armor


@dataclass(frozen=True)
class TestPasskey:
    """A throwaway passkey factor: its published pub and the PRF-output seed.

    The seed stands in for a WebAuthn PRF extension output (that library is out
    of this epic — crib §18). Carried in the clear for the same disposable
    reason as :class:`TestIdentity`.
    """

    factor_id: str
    prf_seed: bytes
    published: PublishedFactor


def make_test_identity(
    *, factor_id: str | None = None, password: str | None = None
) -> TestIdentity:
    """Mint a throwaway password factor. Defaults to random id and password."""
    factor_id = factor_id or f"test-pw-{secrets.token_hex(4)}"
    password = password or f"pw-{secrets.token_hex(8)}"
    factor = create_password_factor(password, factor_id=factor_id)
    return TestIdentity(factor_id, password, factor)


def make_test_passkey(*, factor_id: str | None = None) -> TestPasskey:
    """Mint a throwaway passkey factor with a random PRF-output stand-in."""
    factor_id = factor_id or f"test-pk-{secrets.token_hex(4)}"
    seed = random_seed()
    return TestPasskey(factor_id, seed, create_passkey_factor(seed, factor_id=factor_id))


def make_test_genesis() -> str:
    """A throwaway genesis id — a stand-in for an org object genesis or a
    personal-domain store id."""
    return f"test-genesis-{secrets.token_hex(8)}"
