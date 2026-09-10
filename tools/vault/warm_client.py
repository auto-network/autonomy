"""Headless personal vault unlock using the existing decryption-key handoff.

No personal ledger events or signing delegates are created. Old storage-format
values recover through their persisted descriptors and grants when present.
The personal root stays in this client process, never in the HTTP handoff.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import ssl
import urllib.request

from tools.network.idkit import KeyPair
from tools.network.idkit.root_factor_policy import open_armor_with_password
from tools.network.idkit.canonical import canonical_json
from tools.network.storagekit import credentials as kem_credentials
from tools.network.idkit.sealing import derive_encapsulation_keypair
from tools.vault.personal_object import derive_delegate_audited_recipient

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
    recovery = call("/api/identity/unlock/vault-keys")
    if recovery.get("_status"):
        return recovery
    root_seed = bytes.fromhex(root.private_hex)
    private_hex, public_hex = derive_delegate_audited_recipient(root_seed)
    payload = {
        "generation_keys": {},
        "delegate_audited_private_key": private_hex,
        "delegate_audited_public_key": public_hex,
    }
    genesis_id = recovery.get("recovery_genesis_id")
    if genesis_id:
        kem_private, _ = derive_encapsulation_keypair(
            kem_credentials.derive_kem_seed(root_seed),
            kem_credentials.kem_purpose(genesis_id),
        )
        payload["persona_kem_private_key"] = kem_private
    return call("/api/identity/unlock/vault-keys", payload)


if __name__ == "__main__":
    result = warm(os.environ["PW"])
    print("vault up:", result.get("ok"), "| generations:", result.get("generations"))
    if not result.get("ok"):
        print(result)
        raise SystemExit(1)
