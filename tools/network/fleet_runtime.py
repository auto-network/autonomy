"""Validated browser-to-process credential for unlocked Fleet synchronization.

The browser opens the armored personal root, derives the durable machine key,
and uses that key only long enough to sign a short-lived idkit delegation to a
fresh process key. Python receives the process seed and public certificate,
never the personal root or durable machine seed. Nothing in this module is
persisted; a restart or expiry requires another unlock ceremony.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Iterable

from tools.network import fleet_roster
from tools.network.clock import FLEET_RUNTIME_DELEGATION_TTL_SECONDS
from tools.network.idkit import DelegationCert, KeyPair, verify_chain
from tools.network.idkit.errors import IdkitError


FLEET_SYNC_SCOPE = "fleet:sync"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class FleetRuntimeError(ValueError):
    """A runtime handoff is malformed or not authorized by the live roster."""


@dataclass(frozen=True)
class FleetRuntimeCredential:
    machine_id: str
    machine_pub: str
    process_key: KeyPair
    delegation_cert: DelegationCert

    @classmethod
    def from_browser_payload(
        cls,
        payload: object,
        *,
        personal_root_pub: str,
        roster_entries: Iterable[fleet_roster.RosterEntry],
        now: int | None = None,
    ) -> "FleetRuntimeCredential":
        expected = {
            "machine_id",
            "machine_pub",
            "process_private_seed",
            "delegation_cert",
        }
        if not isinstance(payload, dict) or set(payload) != expected:
            raise FleetRuntimeError(
                f"fleet runtime credential must carry exactly {sorted(expected)}"
            )
        machine_id = _hex(payload["machine_id"], "machine_id")
        machine_pub = _hex(payload["machine_pub"], "machine_pub")
        process_seed = _hex(
            payload["process_private_seed"], "process_private_seed"
        )
        try:
            process_key = KeyPair.from_private_hex(process_seed)
            cert = DelegationCert.from_dict(payload["delegation_cert"])
        except (IdkitError, ValueError, TypeError) as exc:
            raise FleetRuntimeError(f"invalid fleet runtime credential: {exc}") from exc

        anchor = _hex(personal_root_pub, "personal_root_pub")
        entries = tuple(roster_entries)
        active = fleet_roster.resolve(entries, anchor_root_pub=anchor)
        entry = active.get(machine_pub)
        if entry is None or entry.machine_id != machine_id:
            raise FleetRuntimeError(
                "fleet runtime credential is not an active roster machine"
            )
        current = int(time.time()) if now is None else int(now)
        try:
            verified = verify_chain(
                cert,
                machine_pub,
                org=f"personal:{anchor}",
                now=current,
                required_scope=FLEET_SYNC_SCOPE,
            )
        except IdkitError as exc:
            raise FleetRuntimeError(
                f"fleet runtime delegation does not verify: {exc}"
            ) from exc
        if cert.parent_cert is not None:
            raise FleetRuntimeError("fleet runtime delegation must be machine-direct")
        if cert.scope != (FLEET_SYNC_SCOPE,) or cert.target_types is not None:
            raise FleetRuntimeError("fleet runtime delegation has excess authority")
        if (
            verified.subject_kind != "machine"
            or verified.subject_id != machine_id
        ):
            raise FleetRuntimeError("fleet runtime delegation names another machine")
        if verified.leaf_pub != process_key.public_hex:
            raise FleetRuntimeError(
                "fleet runtime process seed does not match its delegation"
            )
        if (
            cert.not_after - cert.not_before
            > FLEET_RUNTIME_DELEGATION_TTL_SECONDS + 60
        ):
            raise FleetRuntimeError("fleet runtime delegation exceeds its TTL bound")
        return cls(machine_id, machine_pub, process_key, cert)


def _hex(value: object, what: str) -> str:
    if not isinstance(value, str) or _HEX64.fullmatch(value) is None:
        raise FleetRuntimeError(f"{what} must be 64 lowercase hex chars")
    return value
