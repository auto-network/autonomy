"""auto.network registry service v1.

FastAPI + SQLite implementation of spec §4 (graph note ``a17c8657-939``):
org bindings, share-link grants, rebind policy, revocations. Every
mutation is authorized by an idkit delegation chain verifying to the org
binding's root key — the registry holds no permission tables (I4).

Bead: ``auto-4p7bg`` (B1). Depends on idkit (``tools.network.idkit``, A1).
"""

from .app import create_app
from .listings import (
    ATTESTATION_DOMAIN,
    LISTING_DOMAIN,
    attestation_id,
    build_attestation_payload,
    build_listing_payload,
    listing_id,
    parse_attestation_record,
    parse_listing_claim,
    sign_attestation_record,
    sign_listing,
)
from .signing import REQUEST_DOMAIN, sign_request

__all__ = [
    "create_app",
    "sign_request",
    "REQUEST_DOMAIN",
    # L1 listing directory wire formats
    "LISTING_DOMAIN",
    "ATTESTATION_DOMAIN",
    "build_listing_payload",
    "sign_listing",
    "parse_listing_claim",
    "listing_id",
    "build_attestation_payload",
    "sign_attestation_record",
    "parse_attestation_record",
    "attestation_id",
]
