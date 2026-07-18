"""Armor tests — the passphrase-encrypted org root key blob (I1)."""

import base64
import json

import pytest

from tools.network.idkit import KeyPair
from tools.network.idkit.armor import (
    ARMOR_BEGIN,
    ARMOR_END,
    ArmorError,
    ArmorPassphraseError,
    decrypt_root_key,
    encrypt_root_key,
    parse_armor,
)


@pytest.fixture(scope="module")
def root() -> KeyPair:
    return KeyPair.generate()


@pytest.fixture(scope="module")
def armor(root: KeyPair) -> str:
    # Floor-of-range iterations keep the suite fast; DEFAULT_ITERATIONS is
    # for real ceremonies.
    return encrypt_root_key(root, "correct horse", iterations=10_000)


def test_roundtrip(root, armor):
    opened = decrypt_root_key(armor, "correct horse")
    assert opened.private_hex == root.private_hex
    assert opened.public_hex == root.public_hex


def test_armor_shape(root, armor):
    lines = armor.splitlines()
    assert lines[0] == ARMOR_BEGIN
    assert lines[-1] == ARMOR_END
    data = parse_armor(armor)
    assert data["v"] == 1
    assert data["kdf"]["iterations"] == 10_000
    assert data["root_pub"] == root.public_hex


def test_armor_never_contains_plaintext(root, armor):
    # I1: neither the raw seed hex nor its base64 appears in the blob.
    assert root.private_hex not in armor
    seed_b64 = base64.b64encode(bytes.fromhex(root.private_hex)).decode()
    assert seed_b64 not in armor


def test_armor_passes_org_key_schema_tripwire(armor):
    # The autonomy.network.org-key schema rejects anything shaped like a
    # raw 64-hex private key; the armor must not trip that.
    from tools.graph.schemas.network_identity import NetworkOrgKeyV1

    NetworkOrgKeyV1.validate({"armored_private_key": armor})


def test_wrong_passphrase(armor):
    with pytest.raises(ArmorPassphraseError):
        decrypt_root_key(armor, "wrong horse")


def test_tampered_ct(armor, root):
    data = parse_armor(armor)
    ct = bytearray(base64.b64decode(data["ct"]))
    ct[0] ^= 0xFF
    data["ct"] = base64.b64encode(bytes(ct)).decode()
    forged = "\n".join(
        [ARMOR_BEGIN, base64.b64encode(json.dumps(data).encode()).decode(), ARMOR_END]
    )
    with pytest.raises(ArmorPassphraseError):
        decrypt_root_key(forged, "correct horse")


def test_relabelled_root_pub_fails_aad(armor):
    # Swapping root_pub breaks GCM AAD binding even with the right
    # passphrase — a blob cannot be re-labelled as another org's key.
    other = KeyPair.generate()
    data = parse_armor(armor)
    data["root_pub"] = other.public_hex
    forged = "\n".join(
        [ARMOR_BEGIN, base64.b64encode(json.dumps(data).encode()).decode(), ARMOR_END]
    )
    with pytest.raises(ArmorPassphraseError):
        decrypt_root_key(forged, "correct horse")


@pytest.mark.parametrize(
    "mangle",
    [
        lambda a: a.replace(ARMOR_BEGIN, "-----BEGIN PGP MESSAGE-----"),
        lambda a: "not an armor at all",
        lambda a: "",
    ],
)
def test_structural_rejections(armor, mangle):
    with pytest.raises(ArmorError):
        decrypt_root_key(mangle(armor), "correct horse")


def test_iteration_floor():
    with pytest.raises(ArmorError):
        encrypt_root_key(KeyPair.generate(), "pp", iterations=9_999)
