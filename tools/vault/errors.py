"""Exceptions for the vault policy-class construction.

Every rejection path raises a distinct subclass so callers — and the
cross-model attack's regression tests — can assert *which* invariant fired
rather than string-matching a message.
"""

from __future__ import annotations


class VaultError(Exception):
    """Base class for every vault policy-class error."""


class PolicyClassError(VaultError):
    """A malformed policy-class record, or an illegal operation on one."""


class FactorError(VaultError):
    """A factor could not be created, published, or opened."""


class PolicyMismatchError(PolicyClassError):
    """A setting's required policy does not match its class's policy.

    Raised at seal time to stop a setting that requires (say) ``both`` from
    being wrapped under a weaker ``password`` class, and at open time when a
    stored policy has been tampered.
    """


class ClassOpenError(PolicyClassError):
    """The policy was not satisfied: no factor secret opened any wrap."""


class FactorIndependenceError(PolicyClassError):
    """Two factors in one class share a public key.

    Raised when a class — most consequentially a ``both`` 2-of-2 class — would
    admit two factors backed by the same key material, which collapses the
    policy (a nominal 2-of-2 that opens with one secret). Cross-model attack
    finding BROKEN-1.
    """


class ConcurrencyError(VaultError):
    """A class write is not an append-only successor of the stored record.

    The store admits only monotonic updates (extend adds wraps to existing
    generations; revoke appends a generation). A write built from a stale read —
    one that would drop a generation or a wrap — is refused rather than
    silently clobbering a concurrent revoke/enroll. Cross-model attack findings
    BROKEN-2 / BROKEN-3; the caller must re-read and retry.
    """
