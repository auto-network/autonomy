"""Headless vault warm-up — the operator's browser flow, driven from Python.

The instrument for a machine whose dashboard restarted with no browser at
hand (the SJC demo box): it does exactly what the sign-on ceremony does, in
order, over HTTP, and nothing else. It requires the operator's password in
``PW`` — possession of the password IS the operator (crib ``1e005d5c-c11``
§3.1: the trust boundary is the machine) — and never persists any derived
key.

    PW='...' python3 -m tools.vault.warm_client

Sequence, matching the ceremony:

1. open the personal armor; prove the password unlock (a session cookie).
2. read the personal ledger heads; mint a fresh MEMORY-class agent delegate
   (a new one every unlock — the private half is not derivable) and post it
   for durability.
3. build the persona's KEM credential (deterministic from the root — same
   bytes on every machine) so the store always holds the recipient a mint's
   self-grant is addressed to. Posting it is idempotent.
4. re-derive the persona KEM private key and hand it, the delegate, and the
   credential to ``/api/identity/unlock/vault-keys``. The dashboard opens
   the persisted grants server-side and rebuilds every generation key the
   process cache lost (tools/vault/unlock.py). §12 permits the dashboard to
   hold exactly these: generation keys, the KEM private, the delegate —
   never the root, which exists here only inside this process and is zeroed
   by exit.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import ssl
import time
import urllib.request

from tools.network.idkit import KeyPair
from tools.network.idkit.root_factor_policy import open_armor_with_password
from tools.network.idkit.canonical import canonical_json
from tools.network.idkit.persona import derive_persona
from tools.network.ledger import HLC, make_event, mint_grant_nonce, sign_delegate_proof
from tools.network.storagekit import credentials as kem_credentials
from tools.network.storagekit.delegate import storage_delegate_scopes
from tools.network.clock import DEFAULT_DELEGATE_TTL_MS

API = os.environ.get("DASHBOARD_API", "https://localhost:8080")
UNLOCK_DOMAIN = b"autonomy.identity.unlock.v1\n"

_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE
_jar = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(
    urllib.request.HTTPSHandler(context=_ctx),
    urllib.request.HTTPCookieProcessor(_jar),
)


def call(path: str, body=None, org: str | None = None, method: str | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        API + path, data=data, method=method or ("POST" if data is not None else "GET")
    )
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if org:
        req.add_header("X-Graph-Org", org)
    try:
        return json.loads(_opener.open(req, timeout=60).read())
    except urllib.error.HTTPError as e:
        return {"_status": e.code, "_body": e.read().decode()[:400]}


def unlock(pw: str) -> KeyPair:
    """Open the armor and prove the password unlock; returns the root."""
    personal = call("/api/identity/personal")
    root = open_armor_with_password(personal["armored_private_key"], pw)
    options = call("/api/identity/unlock/password/options", {})
    call("/api/identity/unlock/password", {
        "challenge": options["challenge"],
        "signature": root.sign_hex(UNLOCK_DOMAIN + canonical_json({
            "v": 1, "challenge": options["challenge"], "origin": options["origin"],
        })),
    })
    return root


def warm(pw: str) -> dict:
    """The whole warm-up. Returns the vault-keys response."""
    root = unlock(pw)
    heads = call("/api/network/ledger/heads?org=personal", org="personal")
    genesis_id, head_ids = heads["genesis_id"], tuple(heads["heads"])
    root_seed = bytes.fromhex(root.private_hex)
    persona = derive_persona(root_seed, genesis_id)

    # Fresh delegate every unlock; consent binds the full grant terms.
    child, nonce = KeyPair.generate(), mint_grant_nonce()
    scopes = storage_delegate_scopes(None)
    delegate_payload = {
        "type": "delegate", "child_pub": child.public_hex, "scope": scopes,
        "can_redelegate": False, "ttl": int(DEFAULT_DELEGATE_TTL_MS),
        "grant_nonce": nonce,
        "proof": sign_delegate_proof(
            child, genesis_id, persona.public_hex, scopes,
            can_redelegate=False, ttl=int(DEFAULT_DELEGATE_TTL_MS),
            grant_nonce=nonce,
        ),
    }
    event = make_event(
        persona, delegate_payload, head_ids, HLC(int(time.time() * 1000), 0)
    )
    granted = call(
        "/api/network/ledger/delegate",
        {"org": "personal", "event": event.to_json().decode()},
        org="personal",
    )
    if granted.get("_status"):
        return granted

    # Deterministic from the root (counter 0): re-built identically on every
    # warm-up; the store dedupes by content address, so re-posting is free.
    kem_seed = kem_credentials.derive_kem_seed(root_seed)
    credential, kem_private = kem_credentials.build(
        persona, genesis_id, kem_seed, head_ids, (int(time.time() * 1000), 0)
    )

    return call("/api/identity/unlock/vault-keys", {
        "generation_keys": {},
        "delegate_signing_key": child.private_hex,
        "kem_credential": credential.to_dict(),
        "persona_kem_private_key": kem_private,
    })


if __name__ == "__main__":
    result = warm(os.environ["PW"])
    print("vault up:", result.get("ok"), "| generations:", result.get("generations"))
    if not result.get("ok"):
        print(result)
        raise SystemExit(1)
