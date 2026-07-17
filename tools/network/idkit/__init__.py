"""idkit — auto.network identity crypto primitives.

Ed25519 keypairs, delegation certificates (canonical-JSON signed, chain
embedded), full chain verification down from an org root public key,
CSPRNG share-link tokens, and root-/parent-signed revocation records.

Pure library: depends on ``cryptography`` only, no service, no I/O.
Spec: graph note ``a17c8657-939`` §2, §3, §7 (invariants I2, I4, I7).
"""

from .canonical import canonical_json
from .certs import (
    CERT_DOMAIN,
    CERT_VERSION,
    MAX_CHAIN_DEPTH,
    SUBJECT_KINDS,
    DelegationCert,
    Subject,
    issue_cert,
)
from .errors import (
    ChainVerifyError,
    ExpiredError,
    IdkitError,
    MalformedError,
    NotYetValidError,
    RevocationAuthorityError,
    RevocationError,
    RevokedError,
    ScopeError,
    ScopeEscalationError,
    SignatureError,
    TTLViolationError,
    WrongOrgError,
)
from .keys import KeyPair, load_public_key, verify_signature
from .revocation import (
    REVOCATION_DOMAIN,
    REVOCATION_VERSION,
    RevocationRecord,
    RevocationSet,
    issue_revocation,
    verify_revocation,
)
from .tokens import TOKEN_BITS, TOKEN_HEX_LEN, generate_token
from .verify import ChainVerifyResult, verify_chain, walk_chain

__all__ = [
    # keys
    "KeyPair",
    "load_public_key",
    "verify_signature",
    # certs
    "DelegationCert",
    "Subject",
    "issue_cert",
    "CERT_DOMAIN",
    "CERT_VERSION",
    "MAX_CHAIN_DEPTH",
    "SUBJECT_KINDS",
    # verify
    "verify_chain",
    "walk_chain",
    "ChainVerifyResult",
    # tokens
    "generate_token",
    "TOKEN_BITS",
    "TOKEN_HEX_LEN",
    # revocation
    "RevocationRecord",
    "RevocationSet",
    "issue_revocation",
    "verify_revocation",
    "REVOCATION_DOMAIN",
    "REVOCATION_VERSION",
    # canonical
    "canonical_json",
    # errors
    "IdkitError",
    "MalformedError",
    "ChainVerifyError",
    "SignatureError",
    "ExpiredError",
    "NotYetValidError",
    "ScopeEscalationError",
    "TTLViolationError",
    "RevokedError",
    "WrongOrgError",
    "ScopeError",
    "RevocationError",
    "RevocationAuthorityError",
]
