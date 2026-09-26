"""Allocation library for administrator-assigned organization serving domains.

Callers must hold registry-administrator authority. The ordinary org tunnel
can consume these records, never create them. No billing rules live here.
"""
from __future__ import annotations

import re
import uuid

BASE_DOMAIN = "serve.auto.network"
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


def validate_managed_domain(domain: str) -> str:
    """Return a normalized single-label platform domain."""
    if not isinstance(domain, str):
        raise ValueError("domain must be a string")
    domain = domain.strip().rstrip(".").lower()
    suffix = "." + BASE_DOMAIN
    label = domain[:-len(suffix)] if domain.endswith(suffix) else ""
    if not _LABEL.fullmatch(label):
        raise ValueError("expected an allocatable <name>.serve.auto.network domain")
    return domain


def is_managed_domain(domain: str) -> bool:
    try:
        return validate_managed_domain(domain) == domain
    except ValueError:
        return False


def show_domain(store, domain: str) -> dict:
    domain = validate_managed_domain(domain)
    row = store.get_serve_zone(domain)
    member_owner = store.get_serving_label_owner(domain.split(".")[0])
    return {"domain": domain, "available": row is None and member_owner is None,
            "reservation": row, "member_owner": member_owner}


def reserve_domain(store, domain: str, org: str, *, now: int) -> dict:
    domain = validate_managed_domain(domain)
    if not isinstance(org, str) or str(uuid.UUID(org)) != org:
        raise ValueError("org must be a canonical registry organization UUID")
    return store.reserve_managed_zone(domain, org=org, now=now)
