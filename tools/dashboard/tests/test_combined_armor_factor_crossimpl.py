"""Cross-impl vector for the root-armor COMBINED (MFA) factor.

The enable-MFA / found-fresh-combined / MFA-unlock ceremony runs in the BROWSER
(that is where the master KEK lives). This drives the REAL
``ceremony/primitives.js`` through node and proves it is byte-compatible with
``idkit.armor``: an armor one side builds a combined factor into, the other side
opens with BOTH the password and the passkey's PRF output — and neither half
alone. If the two ever drifted, a browser could enable MFA the server can never
unlock, or accept an armor it cannot open — the exact silent failure this vector
exists to catch. The require-both guarantee is the linchpin of the factor
system, so it is proven across both implementations here.
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
import {
  encryptArmorCombined, enableMfa, decryptArmorWithCombined, decryptArmor,
  decryptArmorWithPasskey, PASSKEY_ARMOR_PURPOSE,
} from 'file://__CEREMONY__/primitives.js';
import { deriveEncapsulationKeypair } from 'file://__CEREMONY__/sealing.js';

const a = JSON.parse(process.env.CROSSIMPL_ARGS);
const prf = () => Uint8Array.from(Buffer.from(a.prf_hex, 'hex'));
const out = {};
if (a.op === 'kem_pub') {
  out.kem_pub = (await deriveEncapsulationKeypair(prf(), PASSKEY_ARMOR_PURPOSE)).publicKeyHex;
} else if (a.op === 'found_combined') {
  out.armor = await encryptArmorCombined(
    Uint8Array.from(Buffer.from(a.seed_hex, 'hex')), a.root_pub, a.password,
    a.credential_id, a.kem_pub, 10000,
  );
} else if (a.op === 'enable_mfa') {
  out.armor = await enableMfa(a.armor, a.password, a.credential_id, a.kem_pub, 10000);
} else if (a.op === 'open_combined') {
  const r = await decryptArmorWithCombined(a.armor, a.password, prf());
  out.seed_hex = Buffer.from(r.seed).toString('hex');
  out.root_pub = r.rootPub;
} else if (a.op === 'open_password_should_fail') {
  try { await decryptArmor(a.armor, a.password); out.opened = true; }
  catch { out.opened = false; }
} else if (a.op === 'open_passkey_should_fail') {
  try { await decryptArmorWithPasskey(a.armor, prf()); out.opened = true; }
  catch { out.opened = false; }
}
console.log(JSON.stringify(out));
"""


def _js(args: dict) -> dict:
    result = subprocess.run(
        ["node", "--input-type=module", "-e", _JS.replace("__CEREMONY__", _CEREMONY)],
        env={**os.environ, "CROSSIMPL_ARGS": json.dumps(args)},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


_PW = "correct horse battery staple"


def _kem_pub(prf: bytes) -> str:
    from tools.network.idkit.armor import PASSKEY_ARMOR_PURPOSE
    from tools.network.idkit.sealing import derive_encapsulation_keypair
    _, pub = derive_encapsulation_keypair(prf, PASSKEY_ARMOR_PURPOSE)
    return pub


def test_provisioning_pub_agrees_across_impls():
    prf = bytes.fromhex("55") * 32
    assert _js({"op": "kem_pub", "prf_hex": prf.hex()})["kem_pub"] == _kem_pub(prf)


def test_python_found_combined_browser_opens_with_both():
    from tools.network.idkit import KeyPair
    from tools.network.idkit.armor import encrypt_root_key_combined
    root = KeyPair.generate()
    prf = bytes([0x33]) * 32
    armor = encrypt_root_key_combined(root, _PW, "cred-py", _kem_pub(prf), iterations=10_000)
    out = _js({"op": "open_combined", "armor": armor, "password": _PW, "prf_hex": prf.hex()})
    assert out["seed_hex"] == root.private_hex
    assert out["root_pub"] == root.public_hex


def test_browser_found_combined_python_opens_with_both():
    from tools.network.idkit import KeyPair
    from tools.network.idkit.armor import decrypt_root_key_with_combined
    root = KeyPair.generate()
    prf = bytes([0x44]) * 32
    kem = _kem_pub(prf)
    armor = _js({
        "op": "found_combined", "seed_hex": root.private_hex, "root_pub": root.public_hex,
        "password": _PW, "credential_id": "cred-js", "kem_pub": kem, "prf_hex": prf.hex(),
    })["armor"]
    assert decrypt_root_key_with_combined(armor, _PW, prf).private_hex == root.private_hex


def test_python_enable_mfa_browser_opens_and_neither_half_alone():
    from tools.network.idkit import KeyPair
    from tools.network.idkit.armor import (
        add_passkey_factor,
        enable_mfa,
        encrypt_root_key,
    )
    root = KeyPair.generate()
    prf = bytes([0x55]) * 32
    armor = encrypt_root_key(root, _PW, iterations=10_000)
    armor = add_passkey_factor(armor, _PW, "cred-mfa", _kem_pub(prf))
    armor = enable_mfa(armor, _PW, "cred-mfa", _kem_pub(prf), iterations=10_000)
    # the browser opens the python-built MFA armor with BOTH
    out = _js({"op": "open_combined", "armor": armor, "password": _PW, "prf_hex": prf.hex()})
    assert out["seed_hex"] == root.private_hex
    # and neither half alone opens it, in the browser
    assert _js({"op": "open_password_should_fail", "armor": armor, "password": _PW})["opened"] is False
    assert _js({"op": "open_passkey_should_fail", "armor": armor, "prf_hex": prf.hex()})["opened"] is False


def test_browser_enable_mfa_python_opens_and_neither_half_alone():
    from tools.network.idkit import KeyPair
    from tools.network.idkit.armor import (
        add_passkey_factor,
        decrypt_root_key,
        decrypt_root_key_with_combined,
        decrypt_root_key_with_passkey,
        encrypt_root_key,
        ArmorError,
    )
    root = KeyPair.generate()
    prf = bytes([0x66]) * 32
    armor = encrypt_root_key(root, _PW, iterations=10_000)
    armor = add_passkey_factor(armor, _PW, "cred-mfa", _kem_pub(prf))
    armor = _js({
        "op": "enable_mfa", "armor": armor, "password": _PW,
        "credential_id": "cred-mfa", "kem_pub": _kem_pub(prf), "prf_hex": prf.hex(),
    })["armor"]
    # python opens the browser-built MFA armor with BOTH
    assert decrypt_root_key_with_combined(armor, _PW, prf).private_hex == root.private_hex
    # and neither half alone opens it, in python
    with pytest.raises(ArmorError):
        decrypt_root_key(armor, _PW)
    with pytest.raises(ArmorError):
        decrypt_root_key_with_passkey(armor, prf)
