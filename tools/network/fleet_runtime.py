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
import uuid
from dataclasses import dataclass
from typing import Iterable

from tools.network import fleet_roster
from tools.network.clock import FLEET_RUNTIME_DELEGATION_TTL_SECONDS
from tools.network.idkit import DelegationCert, KeyPair, verify_chain
from tools.network.idkit.errors import IdkitError


FLEET_SYNC_SCOPE = "fleet:sync"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")

# Fixed namespace for deriving a personal identity's OWN auto.network org_uuid
# from its root pubkey. The derivation is deterministic so every machine
# computes the SAME personal org_uuid from the same personal root: registering
# the personal org (the "personal tunnel") is then idempotent and needs no
# cross-machine coordination — the registry hands back the UUID when the same
# root re-registers (registry claim_org same-root path). Never regenerate this
# constant; it is the stable anchor of every personal org_uuid in existence.
_PERSONAL_ORG_NS = uuid.UUID("6f2a1e8c-7b3d-5a4f-9c1e-2d8b0a6f3c17")


class FleetRuntimeError(ValueError):
    """A runtime handoff is malformed or not authorized by the live roster."""


def personal_org_uuid(personal_root_pub: str) -> str:
    """The deterministic auto.network org_uuid for a personal identity.

    A personal identity is its own org (the "personal tunnel"), bound to the
    personal root itself. Deriving the org_uuid from the personal root pub keeps
    it stable and re-derivable on any machine without first syncing the binding,
    so registration is idempotent everywhere.
    """
    if not _HEX64.match(personal_root_pub or ""):
        raise FleetRuntimeError("personal_root_pub must be a canonical pubkey")
    return str(uuid.uuid5(_PERSONAL_ORG_NS, personal_root_pub))


@dataclass(frozen=True)
class FleetRuntimeCredential:
    machine_id: str
    machine_pub: str
    process_key: KeyPair
    delegation_cert: DelegationCert
    # Present only when the personal org is registered (has an org_uuid) and the
    # browser therefore minted a reachability cert. Both together or neither: the
    # derived machine key signs node:announce/node:lookup, and the cert (scope
    # node:announce+node:lookup, org=org_uuid) authorizes it. None keeps the
    # credential byte-identical to the sync-only path, so the green sync chain is
    # untouched on an unregistered org.
    machine_key: "KeyPair | None" = None
    reachability_cert: "DelegationCert | None" = None
    #: Per-(org, machine) SERVING key (auto-e2ufw), derived by the browser as
    #: derive_serving_machine_key(root, org_genesis, machine_id) and delivered
    #: only for a serving org. Present only when the payload carries
    #: serving_machine_private_seed; None keeps the fleet key as the hello
    #: machine identity (the transitional path). This is a DISTINCT key from
    #: the fleet machine_key and is used ONLY for the serving tunnel hello,
    #: never for reachability node:announce. The registry's per-org allow-set
    #: (serve_machine_keys) enforces that this pubkey is registered for the
    #: org; from_browser_payload only validates it is a well-formed key.
    serving_machine_key: "KeyPair | None" = None

    @classmethod
    def from_browser_payload(
        cls,
        payload: object,
        *,
        personal_root_pub: str,
        roster_entries: Iterable[fleet_roster.RosterEntry],
        org_uuid: "str | None" = None,
        now: int | None = None,
    ) -> "FleetRuntimeCredential":
        required = {
            "machine_id",
            "machine_pub",
            "process_private_seed",
            "delegation_cert",
        }
        optional = {"machine_private_seed", "reachability_cert",
                    "serving_machine_private_seed"}
        keys = set(payload) if isinstance(payload, dict) else set()
        if not isinstance(payload, dict) or not required <= keys <= (required | optional):
            raise FleetRuntimeError(
                f"fleet runtime credential must carry {sorted(required)} "
                f"(optional {sorted(optional)})"
            )
        if bool("machine_private_seed" in keys) != bool("reachability_cert" in keys):
            raise FleetRuntimeError(
                "reachability requires both machine_private_seed and "
                "reachability_cert, or neither"
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

        machine_key = None
        reachability_cert = None
        if "reachability_cert" in keys:
            # Reachability rides a SEPARATE cert (root-direct, org=the registered
            # org_uuid, node scopes) with the roster machine key as its signer —
            # the registry keys hints by the envelope signer and discovery reads
            # by roster machine_pub, so the announce must be the machine key, not
            # the ephemeral process key. Verified against the personal root, not
            # the sync cert's "personal:<rootpub>" anchor.
            if org_uuid is None:
                raise FleetRuntimeError(
                    "reachability credential delivered without a registered org_uuid"
                )
            try:
                machine_key = KeyPair.from_private_hex(
                    _hex(payload["machine_private_seed"], "machine_private_seed")
                )
                reachability_cert = DelegationCert.from_dict(payload["reachability_cert"])
            except (IdkitError, ValueError, TypeError) as exc:
                raise FleetRuntimeError(
                    f"invalid reachability credential: {exc}"
                ) from exc
            if machine_key.public_hex != machine_pub:
                raise FleetRuntimeError(
                    "reachability machine key is not this roster machine"
                )
            if reachability_cert.child_pub != machine_pub:
                raise FleetRuntimeError("reachability cert names another machine")
            if reachability_cert.parent_cert is not None:
                raise FleetRuntimeError("reachability cert must be root-direct")
            try:
                for required_node_scope in ("node:announce", "node:lookup"):
                    verify_chain(
                        reachability_cert,
                        anchor,
                        org=org_uuid,
                        now=current,
                        required_scope=required_node_scope,
                    )
            except IdkitError as exc:
                raise FleetRuntimeError(
                    f"reachability cert does not verify: {exc}"
                ) from exc

        serving_machine_key = None
        if "serving_machine_private_seed" in keys:
            # The browser derived this per-(org, machine) serving key from the
            # opened root; we only need it well-formed here. The registry's
            # serve_machine_keys allow-set is what binds it to the org
            # (auto-e2ufw Option B), so no roster/genesis check belongs here.
            try:
                serving_machine_key = KeyPair.from_private_hex(
                    _hex(payload["serving_machine_private_seed"],
                         "serving_machine_private_seed")
                )
            except (IdkitError, ValueError, TypeError) as exc:
                raise FleetRuntimeError(
                    f"invalid serving machine key: {exc}"
                ) from exc

        return cls(
            machine_id, machine_pub, process_key, cert,
            machine_key, reachability_cert, serving_machine_key,
        )


def _hex(value: object, what: str) -> str:
    if not isinstance(value, str) or _HEX64.fullmatch(value) is None:
        raise FleetRuntimeError(f"{what} must be 64 lowercase hex chars")
    return value
