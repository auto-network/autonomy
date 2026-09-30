"""Runner offers (bead auto-a51qv): build and verify a machine's
``autonomy.org.session-runner`` row.

A row is self-certified the way an org reachability row is
(fleet_org_reachability.sign_certified / verify_certified): the member
persona's certificate to the machine's per-organization serving key, and that
key's signature over the row. A reader keeps a row only when its evidence
verifies and its persona is still a member.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from tools.graph.schemas.org_session_runner import (
    ADMIT_ALL_MEMBERS,
    ROW_VERSION,
    OrgSessionRunnerV1,
)
from tools.network.fleet_org_reachability import sign_certified, verify_certified
from tools.network.idkit import DelegationCert, KeyPair

ROW_DOMAIN = b"autonomy.network.org-session-runner.row.v1\n"


def build_row(machine_key: KeyPair, persona_cert: DelegationCert, *, label: str,
              capacity: int, harnesses: list[str], images: list[str],
              now: int | None = None) -> dict[str, Any]:
    """This machine's offer, signed by its serving key. Raises
    SchemaValidationError on a bad shape."""
    body = {
        "v": ROW_VERSION,
        "machine_pub": machine_key.public_hex,
        "persona_pub": str(persona_cert.subject.id),
        "persona_cert": persona_cert.to_dict(),
        "label": label,
        "capacity": int(capacity),
        "harnesses": list(harnesses),
        "images": sorted(set(images)),
        "admit": ADMIT_ALL_MEMBERS,
        "updated_at": int(time.time() if now is None else now),
    }
    body["sig"] = sign_certified(machine_key, body, ROW_DOMAIN)
    # The key carries machine_pub; verify_row puts it back before checking.
    row = {k: v for k, v in body.items() if k != "machine_pub"}
    OrgSessionRunnerV1.validate(row)
    return row


def verify_row(key: str, payload: Any, *, org: str, now: int | None = None,
               is_member: Callable[[str], bool | None] | None = None) -> dict | None:
    """The offer (with ``machine_pub``) when the row verifies, else None."""
    try:
        OrgSessionRunnerV1.validate(payload)
    except Exception:
        return None
    payload = {**dict(payload), "machine_pub": key}
    persona = verify_certified(key, payload, org=org, domain=ROW_DOMAIN, now=now)
    if persona is None:
        return None
    if is_member is not None and is_member(persona) is False:
        return None
    return payload
