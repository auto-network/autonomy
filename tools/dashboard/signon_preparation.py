"""Prepare encrypted root-ceremony inputs using the existing public recipient."""
import json
import logging
import time

from starlette.responses import JSONResponse

from tools.graph import org_ops
from tools.graph.schemas.network_identity import NETWORK_ORG_KEY_SET_ID
from tools.graph.schemas.vault_policy_class import VAULT_POLICY_CLASS_SET_ID
from tools.network.idkit import sealing
from tools.vault.key_holder import _scoped_db
from tools.vault.store import VaultStore

PURPOSE = "autonomy/identity/sign-in-preparation/v1"
logger = logging.getLogger(__name__)


def _body(response):
    result = json.loads(response.body)
    if response.status_code >= 400:
        raise ValueError(result.get("error", "preparation unavailable"))
    return result


def organization_plans():
    """The registered, locally held organizations serviced by root sign-in."""
    from tools.dashboard import network_routes
    from tools.data_paths import LOCAL_STORE_KEYS

    for ref in org_ops.list_orgs():
        if ref.slug in LOCAL_STORE_KEYS:
            continue
        entry = network_routes._org_unlock_plan(ref.slug, ref.slug, LOCAL_STORE_KEYS)
        if not entry.get("committed_membership_org") or not entry.get("genesis_id"):
            continue
        persona_pub = org_ops.persona_pub_for_org(entry["genesis_id"])
        if persona_pub:
            yield entry, persona_pub


def collect():
    from tools.dashboard import (fleet_enrollment_routes as fleet, identity_routes,
                                 membership_checkpoint, network_routes, unlock_routes,
                                 vault_routes, org_storage_delegate)

    personal = identity_routes._personal_member()
    vault = _body(unlock_routes.personal_vault_recovery())
    vault.update(root_pub=personal.payload["root_pub"],
                 inventory=vault_routes.root_anchor_inventory())
    runtime = _body(fleet.runtime_preparation())
    _, recovery, delivery = fleet._local_completion_state()
    completion = None
    if recovery is not None and delivery is not None:
        completion = {"request_id": recovery.request_id,
                      "request": recovery.request.to_dict(),
                      "channel_binding": recovery.channel_binding,
                      "approval": delivery.approval.to_dict(),
                      "roster_entry": delivery.roster_entry.to_dict()}
    organizations = []
    for entry, persona_pub in organization_plans():
        org = entry["slug"]
        entry["storage_delegate"] = org_storage_delegate.prepare(org)
        okey = network_routes._first_member(NETWORK_ORG_KEY_SET_ID, org)
        entry["org_key"] = okey.payload if okey else None
        entry["checkpoint_work"] = None
        if entry["checkpoint"]["needed"]:
            decision = membership_checkpoint.checkpoint_due(org, persona_pub,
                ts=int(time.time()), genesis_id=entry["genesis_id"], org_uuid=entry["org_uuid"])
            if decision.action == "assemble":
                entry["checkpoint_work"] = {"record": decision.record, "sign_with": decision.sign_with}
        organizations.append(entry)
    # A personal serving certificate status read is local and needs no signer.
    from tools.dashboard.link_serving_supervisor import serve_cert_state
    return {"vault": vault, "runtime": runtime, "completion": completion,
            "personal_serve": serve_cert_state(None), "organizations": organizations}


async def get_preparation(request):
    """Pre-authentication response contains ciphertext only, never org metadata."""
    try:
        with VaultStore(_scoped_db(VAULT_POLICY_CLASS_SET_ID, None)) as store:
            public = store.get_delegate_audited_recipient()
        payload = json.dumps(collect(), separators=(",", ":")).encode()
        return JSONResponse({"sealed": sealing.seal(payload, public, PURPOSE).hex()},
                            headers={"Cache-Control": "no-store"})
    except Exception:
        # Do not disclose which organization or private preparation read failed.
        logger.exception("sign-in preparation failed")
        return JSONResponse({"error": "sign-in preparation unavailable"}, status_code=503)
