"""Org-peer discovery as replicated rows (auto-mldvv, design graph://c2baad48-0a3 §4).

The organization's machines keep their own directory: one row per machine
in the org-homed set ``autonomy.org.fleet-reachability#1``, written by that
machine only when its address set changes, replicated to every member's
machines by the same org-scope sync that carries everything else. A reader
verifies each row itself -- the persona certificate to the machine key,
the machine key's signature over the row, and the persona's presence in
the adopted member set -- and treats the addresses as hints: the org hello
decides admission, and a dead address costs one bounded connect attempt.

First contact (a machine holding no rows yet) comes from the scheduler's
``org_peer_addresses`` hook -- another machine of the same member, or the
join ceremony's rendezvous -- and the rows take over from the first pull.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

from tools.graph.schemas.org_fleet_reachability import (
    MAX_ADDRESSES,
    ORG_FLEET_REACHABILITY_REVISION as REVISION,
    ORG_FLEET_REACHABILITY_SET_ID as SET_ID,
    ROW_VERSION,
    OrgFleetReachabilityV1,
)
from tools.network.idkit import DelegationCert, IdkitError, KeyPair, canonical_json
from tools.network.idkit.keys import verify_signature
from tools.network.idkit.verify import verify_chain

logger = logging.getLogger(__name__)

ROW_DOMAIN = b"autonomy.network.fleet-org-reachability.row.v1\n"
ORG_SYNC_SCOPE = "fleet:sync"


def _signing_input(body: dict[str, Any]) -> bytes:
    return ROW_DOMAIN + canonical_json({k: v for k, v in body.items() if k != "sig"})


def build_row(
    machine_key: KeyPair,
    persona_cert: DelegationCert,
    addresses: Sequence[str],
    *,
    now: int | None = None,
) -> dict[str, Any]:
    """This machine's row: its addresses under its persona's certificate,
    signed by the machine key. Raises SchemaValidationError on a bad shape."""
    body = {
        "v": ROW_VERSION,
        "machine_pub": machine_key.public_hex,
        "persona_pub": str(persona_cert.subject.id),
        "persona_cert": persona_cert.to_dict(),
        "addresses": list(dict.fromkeys(addresses))[:MAX_ADDRESSES],
        "updated_at": int(time.time() if now is None else now),
    }
    body["sig"] = machine_key.sign_hex(_signing_input(body))
    OrgFleetReachabilityV1.validate(body)
    return body


def verify_row(
    key: str,
    payload: Any,
    *,
    org: str,
    now: int | None = None,
    is_member: Callable[[str], bool | None] | None = None,
) -> tuple[str, list[str]] | None:
    """``(persona_pub, addresses)`` for a row that verifies, else None.

    In order: shape; key names the machine; the persona certificate is for
    this org, scope fleet:sync, and its chain ends at the machine key; the
    machine key signed the row; the persona is in the adopted member set
    (``is_member`` -> False drops the row; None means unknown and the row
    stands as a hint, admission being the hello's).
    """
    try:
        OrgFleetReachabilityV1.validate(payload)
    except Exception:
        return None
    if payload["machine_pub"] != key:
        return None
    try:
        cert = DelegationCert.from_dict(payload["persona_cert"])
    except (IdkitError, ValueError, TypeError, KeyError):
        return None
    if cert.org != org or cert.subject.kind != "persona" \
            or str(cert.subject.id) != payload["persona_pub"]:
        return None
    try:
        verified = verify_chain(
            cert, payload["persona_pub"], org=org,
            now=int(time.time() if now is None else now),
            required_scope=ORG_SYNC_SCOPE,
        )
        if verified.leaf_pub != key:
            return None
        verify_signature(key, payload["sig"], _signing_input(payload))
    except IdkitError:
        return None
    if is_member is not None and is_member(payload["persona_pub"]) is False:
        return None
    return payload["persona_pub"], list(payload["addresses"])


def read_rows(path: Path | str) -> dict[str, Any]:
    """key -> payload of the set's base rows in the org database at *path*
    (read-only; the newest row per key wins). Missing store: {}."""
    path = Path(path)
    if not path.exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        rows = conn.execute(
            'SELECT "key",payload FROM settings WHERE set_id=? AND schema_revision=? '
            "AND supersedes IS NULL AND excludes IS NULL AND deprecated=0 "
            "ORDER BY updated_at, created_at",
            (SET_ID, REVISION),
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    out: dict[str, Any] = {}
    for key, payload in rows:
        try:
            out[str(key)] = json.loads(payload) if isinstance(payload, str) else payload
        except (ValueError, TypeError):
            continue
    return out


def co_member_addresses(
    path: Path | str,
    *,
    org: str,
    own_machine_pub: str,
    is_member: Callable[[str], bool | None] | None = None,
    now: int | None = None,
) -> dict[str, list[str]]:
    """machine_pub -> addresses for every verified row in the org database
    at *path* other than this machine's own."""
    peers: dict[str, list[str]] = {}
    for key, payload in read_rows(path).items():
        if key == own_machine_pub:
            continue
        verified = verify_row(key, payload, org=org, now=now, is_member=is_member)
        if verified is None or not verified[1]:
            continue
        peers[key] = verified[1]
    return peers


def _stored_addresses(slug: str, machine_pub: str) -> list[str] | None:
    from tools.graph import settings_ops

    try:
        members = settings_ops.read_owned_set(
            SET_ID, org=slug, target_revision=REVISION,
        ).to_dict()
    except Exception:
        return None
    member = members.get(machine_pub)
    if member is None:
        return None
    payload = member.payload or {}
    addresses = payload.get("addresses")
    return list(addresses) if isinstance(addresses, list) else None


def publish_if_changed(
    slug: str,
    machine_key: KeyPair,
    persona_cert: DelegationCert,
    addresses: Sequence[str],
    *,
    now: int | None = None,
) -> bool:
    """Write this machine's row into org *slug*'s database ONLY when the
    address set differs from the stored row (or there is none). Returns
    True when a row was written. Empty addresses publish nothing new but
    do replace a stale non-empty row, so a machine that stopped listening
    stops being dialled."""
    wanted = list(dict.fromkeys(addresses))[:MAX_ADDRESSES]
    stored = _stored_addresses(slug, machine_key.public_hex)
    if stored is not None and stored == wanted:
        return False
    if stored is None and not wanted:
        return False
    from tools.graph import settings_ops

    row = build_row(machine_key, persona_cert, wanted, now=now)
    settings_ops.upsert_by_key(
        SET_ID, REVISION, machine_key.public_hex, row, org=slug, state="published",
    )
    return True


def digest_addresses(addresses: Iterable[str]) -> str:
    """A stable fingerprint of an address set, for change detection."""
    return hashlib.sha256(canonical_json(list(addresses))).hexdigest()
