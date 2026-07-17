"""Exception taxonomy for idkit.

Every rejection path raises a distinct subclass so callers (and tests) can
pin the *reason* a chain or revocation was refused, not just that it was.
All of them derive from :class:`IdkitError`.
"""

from __future__ import annotations


class IdkitError(Exception):
    """Base class for all idkit errors."""


class MalformedError(IdkitError):
    """Input could not be parsed as a structurally valid idkit object."""


class ChainVerifyError(IdkitError):
    """Base class for delegation-chain verification failures."""


class SignatureError(ChainVerifyError):
    """A signature did not verify against the expected signer key."""


class ExpiredError(ChainVerifyError):
    """A hop's ``not_after`` is in the past."""


class NotYetValidError(ChainVerifyError):
    """A hop's ``not_before`` is in the future."""


class ScopeEscalationError(ChainVerifyError):
    """A child hop claims scope its parent does not hold (or fails to narrow)."""


class TTLViolationError(ChainVerifyError):
    """A child hop's validity window is not strictly inside its parent's."""


class RevokedError(ChainVerifyError):
    """A key id appearing anywhere in the chain has been revoked."""


class WrongOrgError(ChainVerifyError):
    """A hop names a different org than the one being verified against."""


class ScopeError(ChainVerifyError):
    """The leaf certificate does not carry a required scope/target type."""


class RevocationError(IdkitError):
    """Base class for revocation-record verification failures."""


class RevocationAuthorityError(RevocationError):
    """The revocation issuer lacks authority over the revoked key."""
