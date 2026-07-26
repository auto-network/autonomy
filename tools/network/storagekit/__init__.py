"""storagekit — storage key-control records for persistent content encryption.

The signed-record family that protects an organization's persistent
content: persona key-encapsulation credentials, storage-state
descriptors, parent-secret bridges, capability grants and receipts.
These records are not authority events — they are verified against the
authority fold but live outside its vocabulary (contract §1; graph
source ``bb971a32-ed1``, revision 2).

This package provides the shared foundation: the error hierarchy, the
per-position suite identifiers with fail-closed recognition, and the
canonical signed-record helpers every record module reuses.

Pure library: depends on ``idkit`` and the standard library only.
"""

from .credentials import domain_member_keys, select_current_credential
from .errors import (
    CommitmentError,
    MalformedRecordError,
    RecordSignatureError,
    StorageError,
    SuiteError,
)
from .records import parse_canonical, record_id, signing_input
from .suites import (
    BODY_SUITE_CHUNKED_RESERVED,
    BODY_SUITE_DEFAULT,
    BODY_SUITE_LARGE,
    BODY_SUITES,
    HASH_SUITE,
    HASH_SUITES,
    SEAL_SUITE,
    SEAL_SUITES,
    SIGNATURE_SUITE,
    SIGNATURE_SUITES,
    WRAP_SUITE,
    WRAP_SUITES,
    require_suite,
)

__all__ = [
    # credentials
    "domain_member_keys",
    "select_current_credential",
    # errors
    "StorageError",
    "MalformedRecordError",
    "RecordSignatureError",
    "SuiteError",
    "CommitmentError",
    # records
    "signing_input",
    "record_id",
    "parse_canonical",
    # suites
    "HASH_SUITE",
    "SIGNATURE_SUITE",
    "SEAL_SUITE",
    "WRAP_SUITE",
    "BODY_SUITE_DEFAULT",
    "BODY_SUITE_LARGE",
    "BODY_SUITE_CHUNKED_RESERVED",
    "HASH_SUITES",
    "SIGNATURE_SUITES",
    "SEAL_SUITES",
    "WRAP_SUITES",
    "BODY_SUITES",
    "require_suite",
]
