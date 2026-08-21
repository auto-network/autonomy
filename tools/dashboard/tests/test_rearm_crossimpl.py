"""Cross-impl vector for the browser re-arm flow.

The factor-management UI re-factors the root armor in the BROWSER and posts a
root signature the server verifies (POST /api/identity/personal/armor). This
drives the REAL ceremony/primitives.js buildReArm through node and proves the
body it produces is exactly what the server accepts: the signature verifies
against the root under idkit's REARMOR_DOMAIN, and the re-factored armor is a
valid armor that still opens the SAME identity. If the two ever drifted, the
operator's promote/demote/removePassword would be silently rejected — or worse,
accepted with an armor the server could not later open.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not on PATH"
)

_CEREMONY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "static", "js", "ceremony",
)

_JS = r"""
import { signArmorUpdate } from 'file://__CEREMONY__/primitives.js';
const a = JSON.parse(process.env.CROSSIMPL_ARGS);
const body = await signArmorUpdate(a.armor, a.password, a.action, a.require_pair);
console.log(JSON.stringify(body));
"""

_PW = "correct horse battery staple"


def _js_rearm(args: dict) -> dict:
    result = subprocess.run(
        ["node", "--input-type=module", "-e", _JS.replace("__CEREMONY__", _CEREMONY)],
        env={**os.environ, "CROSSIMPL_ARGS": json.dumps(args)},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _accepts(root, body):
    """The server's acceptance criteria for a re-arm, applied directly."""
    from tools.dashboard.identity_routes import REARMOR_DOMAIN
    from tools.network.idkit.armor import armor_root_pub, canonicalize_armor
    from tools.network.idkit.keys import verify_signature
    canonical = canonicalize_armor(body["armored_private_key"])
    assert armor_root_pub(canonical) == root.public_hex
    verify_signature(root.public_hex, body["signature"],
                     REARMOR_DOMAIN + canonical.encode("utf-8"))  # raises if bad


def _provisioning_pub(prf: bytes) -> str:
    from tools.network.idkit.armor import PASSKEY_ARMOR_PURPOSE
    from tools.network.idkit.sealing import derive_encapsulation_keypair
    _, pub = derive_encapsulation_keypair(prf, PASSKEY_ARMOR_PURPOSE)
    return pub


def test_browser_promote_is_server_accepted():
    from tools.network.idkit import KeyPair
    from tools.network.idkit.armor import (
        armor_factor_types, decrypt_root_key, decrypt_root_key_with_passkey,
        encrypt_root_key,
    )
    root = KeyPair.generate()
    armor = encrypt_root_key(root, _PW, iterations=10_000)
    prf = bytes([0x21]) * 32
    body = _js_rearm({
        "armor": armor, "password": _PW,
        "action": {"kind": "promote", "credentialId": "cred-a",
                   "provisioningPub": _provisioning_pub(prf)},
    })
    _accepts(root, body)  # signature + identity check the server does
    new_armor = body["armored_private_key"]
    assert "passkey" in armor_factor_types(new_armor)
    # the re-factored armor still opens the SAME identity, by password AND passkey
    assert decrypt_root_key(new_armor, _PW).private_hex == root.private_hex
    assert decrypt_root_key_with_passkey(new_armor, prf).private_hex == root.private_hex


def test_browser_remove_password_leaves_a_passkey_only_identity():
    from tools.network.idkit import KeyPair
    from tools.network.idkit.armor import (
        add_passkey_factor, armor_factor_types, decrypt_root_key_with_passkey,
        encrypt_root_key,
    )
    root = KeyPair.generate()
    prf = bytes([0x22]) * 32
    # start password + passkey, then the browser removes the password
    armor = add_passkey_factor(
        encrypt_root_key(root, _PW, iterations=10_000), _PW, "cred-a",
        _provisioning_pub(prf))
    body = _js_rearm({
        "armor": armor, "password": _PW, "action": {"kind": "removePassword"}})
    _accepts(root, body)
    new_armor = body["armored_private_key"]
    assert armor_factor_types(new_armor) == ["passkey"]
    assert decrypt_root_key_with_passkey(new_armor, prf).private_hex == root.private_hex


def test_browser_set_authority_carries_require_pair():
    from tools.network.idkit import KeyPair
    from tools.network.idkit.armor import add_passkey_factor, encrypt_root_key
    root = KeyPair.generate()
    armor = add_passkey_factor(
        encrypt_root_key(root, _PW, iterations=10_000), _PW, "cred-a",
        _provisioning_pub(bytes([0x23]) * 32))
    body = _js_rearm({
        "armor": armor, "password": _PW,
        "action": {"kind": "setAuthority"}, "require_pair": True})
    _accepts(root, body)
    assert body["require_pair"] is True
