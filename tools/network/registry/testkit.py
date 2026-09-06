"""Test support: seed a link grant directly in a registry store.

Link publish rides the org's authenticated serving tunnel in production
(D19 / auto-qol1v) — there is no HTTP mint route. Suites whose subject is
serving, relaying, or resolving an EXISTING grant seed it here, written
exactly the way the tunnel control op records it: attributed to the org
tunnel, no persona (``signer_pub``/``subject_id`` are NULL). The mint path
itself is pinned in ``tools/network/registry/tests/test_tunnel_control.py``.
"""

from __future__ import annotations

import secrets
import time

from tools.network.registry.store import LinkGrant, RegistryStore


def mint_store_link(
    store: RegistryStore,
    org_uuid: str,
    target_uuid: str,
    target_type: str = "present",
    *,
    meta: dict | None = None,
    now: int | None = None,
    expires_at: int | None = None,
    expires_at_ms: int | None = None,
    invite_ref: str | None = None,
) -> str:
    """Insert one org-tunnel-attributed grant; returns its token."""
    token = secrets.token_hex(16)
    store.create_link(LinkGrant(
        token=token,
        org_uuid=org_uuid,
        target_uuid=target_uuid,
        target_type=target_type,
        meta=meta or {},
        created_at=now if now is not None else int(time.time()),
        expires_at=expires_at,
        revoked_at=None,
        signer_pub=None,
        subject_kind="org-tunnel",
        subject_id=None,
        invite_ref=invite_ref,
        expires_at_ms=expires_at_ms,
    ))
    return token


def mint_link_at(db_path, org_uuid: str, target_uuid: str, **kwargs) -> str:
    """Mint against a registry database FILE another process is serving
    (socket-based test stacks): open a second store connection, insert,
    close. SQLite serializes the write against the server's connection."""
    store = RegistryStore(str(db_path))
    try:
        return mint_store_link(store, org_uuid, target_uuid, **kwargs)
    finally:
        store.close()
