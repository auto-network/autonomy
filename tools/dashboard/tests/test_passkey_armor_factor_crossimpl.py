"""Cross-impl vector for the root-armor passkey factor.

The sign-on/unlock UI runs the promote/demote/passkey-unlock ceremony in the
BROWSER (that is where the master KEK lives). This drives the REAL
``ceremony/primitives.js`` through node and proves it is byte-compatible with
``idkit.armor``: an armor one side seals a passkey factor into, the other side
opens with that passkey's PRF output — both directions. If the two ever drifted,
a browser could promote a passkey the server can never unlock with, or accept an
armor it cannot open — the exact silent failure this vector exists to catch.
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
  addPasskeyFactor, decryptArmorWithPasskey, decryptArmor, PASSKEY_ARMOR_PURPOSE,
} from 'file://__CEREMONY__/primitives.js';
import { deriveEncapsulationKeypair } from 'file://__CEREMONY__/sealing.js';

const a = JSON.parse(process.env.CROSSIMPL_ARGS);
const prf = () => Uint8Array.from(Buffer.from(a.prf_hex, 'hex'));
const out = {};
if (a.op === 'kem_pub') {
  out.kem_pub = (await deriveEncapsulationKeypair(prf(), PASSKEY_ARMOR_PURPOSE)).publicKeyHex;
} else if (a.op === 'add') {
  out.armor = await addPasskeyFactor(a.armor, a.password, a.credential_id, a.kem_pub);
} else if (a.op === 'open_passkey') {
  const r = await decryptArmorWithPasskey(a.armor, prf());
  out.seed_hex = Buffer.from(r.seed).toString('hex');
  out.root_pub = r.rootPub;
} else if (a.op === 'open_password') {
  const r = await decryptArmor(a.armor, a.password);
  out.seed_hex = Buffer.from(r.seed).toString('hex');
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


def _new_armor():
    from tools.network.idkit import KeyPair
    from tools.network.idkit.armor import encrypt_root_key
    root = KeyPair.generate()
    return root, encrypt_root_key(root, _PW, iterations=10_000)


def _kem_pub(prf: bytes) -> str:
    from tools.network.idkit.armor import PASSKEY_ARMOR_PURPOSE
    from tools.network.idkit.sealing import derive_encapsulation_keypair
    _, pub = derive_encapsulation_keypair(prf, PASSKEY_ARMOR_PURPOSE)
    return pub


def test_provisioning_pub_agrees_across_impls():
    prf = bytes.fromhex("55") * 32
    assert _js({"op": "kem_pub", "prf_hex": prf.hex()})["kem_pub"] == _kem_pub(prf)


def test_python_seals_browser_opens_with_passkey():
    from tools.network.idkit.armor import add_passkey_factor
    root, armor = _new_armor()
    prf = bytes([0x33]) * 32
    armor2 = add_passkey_factor(armor, _PW, "cred-py", _kem_pub(prf))
    out = _js({"op": "open_passkey", "armor": armor2, "prf_hex": prf.hex()})
    assert out["seed_hex"] == root.private_hex
    assert out["root_pub"] == root.public_hex


def test_browser_seals_python_opens_with_passkey():
    from tools.network.idkit.armor import (
        decrypt_root_key,
        decrypt_root_key_with_passkey,
    )
    root, armor = _new_armor()
    prf = bytes([0x44]) * 32
    kem = _js({"op": "kem_pub", "prf_hex": prf.hex()})["kem_pub"]
    armor3 = _js({
        "op": "add", "armor": armor, "password": _PW,
        "credential_id": "cred-js", "kem_pub": kem,
    })["armor"]
    # the passkey opens it (passkey-only unlock)
    assert decrypt_root_key_with_passkey(armor3, prf).private_hex == root.private_hex
    # and the password still opens the browser-produced armor
    assert decrypt_root_key(armor3, _PW).private_hex == root.private_hex


def test_python_sealed_armor_still_opens_with_password_in_browser():
    from tools.network.idkit.armor import add_passkey_factor
    root, armor = _new_armor()
    prf = bytes([0x66]) * 32
    armor2 = add_passkey_factor(armor, _PW, "cred-both", _kem_pub(prf))
    out = _js({"op": "open_password", "armor": armor2, "password": _PW,
               "prf_hex": prf.hex()})
    assert out["seed_hex"] == root.private_hex


def test_same_credential_two_devices_each_prf_opens_crossimpl():
    """One synced credential, two device PRFs -> two slots; each device's PRF
    opens the armor. The passkey PRF is device-specific even for an iCloud-synced
    credential, so 'the same passkey' must hold one factor slot per device, and
    both impls must parse an armor that carries two slots sharing a credential."""
    from tools.network.idkit.armor import (
        add_passkey_factor,
        decrypt_root_key_with_passkey,
    )
    root, armor = _new_armor()
    prf_a = bytes([0x11]) * 32
    prf_b = bytes([0x22]) * 32
    cred = "cred-synced"  # SAME credential on both devices
    armor_a = add_passkey_factor(armor, _PW, cred, _kem_pub(prf_a))
    armor_ab = add_passkey_factor(armor_a, _PW, cred, _kem_pub(prf_b))
    # Both device PRFs open it, in Python and in the browser (find-by-kem_pub
    # selects the right slot from what each device's ceremony produces).
    assert decrypt_root_key_with_passkey(armor_ab, prf_a).private_hex == root.private_hex
    assert decrypt_root_key_with_passkey(armor_ab, prf_b).private_hex == root.private_hex
    assert _js({"op": "open_passkey", "armor": armor_ab,
                "prf_hex": prf_a.hex()})["seed_hex"] == root.private_hex
    assert _js({"op": "open_passkey", "armor": armor_ab,
                "prf_hex": prf_b.hex()})["seed_hex"] == root.private_hex


def test_browser_enrolls_second_device_slot_python_opens_both():
    """The blocker this vector guards: the browser must let the SAME credential
    enroll a SECOND device slot (a new PRF), not reject it as a duplicate."""
    from tools.network.idkit.armor import decrypt_root_key_with_passkey
    root, armor = _new_armor()
    prf_a = bytes([0x77]) * 32
    prf_b = bytes([0x88]) * 32
    cred = "cred-synced-js"
    kem_a = _js({"op": "kem_pub", "prf_hex": prf_a.hex()})["kem_pub"]
    kem_b = _js({"op": "kem_pub", "prf_hex": prf_b.hex()})["kem_pub"]
    armor_a = _js({"op": "add", "armor": armor, "password": _PW,
                   "credential_id": cred, "kem_pub": kem_a})["armor"]
    armor_ab = _js({"op": "add", "armor": armor_a, "password": _PW,
                    "credential_id": cred, "kem_pub": kem_b})["armor"]
    assert decrypt_root_key_with_passkey(armor_ab, prf_a).private_hex == root.private_hex
    assert decrypt_root_key_with_passkey(armor_ab, prf_b).private_hex == root.private_hex


def test_true_duplicate_same_credential_and_prf_rejected_crossimpl():
    """A true duplicate — same credential AND same device PRF — is still an error
    in both impls (that is a rotation, not a new device slot)."""
    from tools.network.idkit.armor import ArmorError, add_passkey_factor
    _, armor = _new_armor()
    prf = bytes([0x99]) * 32
    cred = "cred-dup"
    armor2 = add_passkey_factor(armor, _PW, cred, _kem_pub(prf))
    with pytest.raises(ArmorError):
        add_passkey_factor(armor2, _PW, cred, _kem_pub(prf))
    # the browser rejects the true duplicate too: addPasskeyFactor throws, node
    # exits nonzero, and _js asserts on returncode.
    with pytest.raises(AssertionError):
        _js({"op": "add", "armor": armor2, "password": _PW,
             "credential_id": cred, "kem_pub": _kem_pub(prf)})
