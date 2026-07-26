"""Exception taxonomy for storagekit.

Every rejection path raises a distinct subclass so callers (and tests)
can pin the *reason* a record was refused. All derive from
:class:`StorageError`; record modules raise these or module-local
subclasses of :class:`StorageError`.
"""

from __future__ import annotations


class StorageError(Exception):
    """Base class for all storagekit errors."""


class MalformedRecordError(StorageError):
    """Input could not be parsed as a structurally valid storage record."""


class RecordSignatureError(StorageError):
    """A record signature did not verify against the expected signer key."""


class SuiteError(StorageError):
    """A suite identifier is outside the recognized set — fails closed."""


class CommitmentError(StorageError):
    """A secret does not match the commitment a record carries."""
