"""Armor tests — the password-encrypted root key blob (I1).

One armor format: a master KEK wrapped by a factor list, with the seed
sealed under it. There is no second version to dispatch between, so these
test the format rather than a negotiation between formats.
"""

import base64
import json

import pytest

from tools.network.idkit import KeyPair
from tools.network.idkit.errors import MalformedError
from tools.network.idkit.armor import (
    ARMOR_BEGIN,
    ARMOR_END,
    PASSKEY_ARMOR_PURPOSE,
    ArmorError,
    ArmorPassphraseError,
    add_passkey_factor,
    armor_factor_types,
    armor_root_pub,
    armor_version,
    canonicalize_armor,
    decrypt_root_key,
    decrypt_root_key_with_passkey,
    encrypt_root_key,
    parse_armor,
    remove_passkey_factor,
)
from tools.network.idkit.sealing import derive_encapsulation_keypair


@pytest.fixture(scope="module")
def root() -> KeyPair:
    return KeyPair.generate()


_PW = "correct horse battery staple"


@pytest.fixture(scope="module")
def armor(root: KeyPair) -> str:
    return encrypt_root_key(root, _PW, iterations=10_000)


def test_roundtrip(root, armor):
    opened = decrypt_root_key(armor, _PW)
    assert opened.private_hex == root.private_hex
    assert opened.public_hex == root.public_hex


def test_shape_is_versioned_factor_list(root, armor):
    data = parse_armor(armor)
    assert data["v"] == 2
    assert set(data) == {"v", "root_pub", "kek_seal", "factors"}
    assert data["root_pub"] == root.public_hex
    assert [f["type"] for f in data["factors"]] == ["password"]


def test_never_contains_plaintext(root, armor):
    assert root.private_hex not in armor
    body = base64.b64decode(
        "".join(ln for ln in armor.splitlines() if ln and "-----" not in ln)
    )
    assert bytes.fromhex(root.private_hex) not in body


def test_wrong_passphrase(armor):
    with pytest.raises(ArmorPassphraseError):
        decrypt_root_key(armor, "not the passphrase")


def _v2_body(armor: str) -> dict:
    lines = [ln for ln in armor.strip().splitlines() if ln.strip()]
    return json.loads(base64.b64decode("".join(lines[1:-1])))


def _reseal(body: dict) -> str:
    b64 = base64.b64encode(json.dumps(body, separators=(",", ":")).encode()).decode()
    return "\n".join([ARMOR_BEGIN, b64, ARMOR_END])


def test_extra_toplevel_field_refused_I1(armor):
    body = _v2_body(armor)
    body["smuggled"] = "AAAA"
    with pytest.raises(ArmorError, match="I1|unknown fields"):
        parse_armor(_reseal(body))


def test_extra_field_in_kek_seal_refused(armor):
    body = _v2_body(armor)
    body["kek_seal"]["extra"] = "AAAA"
    with pytest.raises(ArmorError):
        parse_armor(_reseal(body))


def test_extra_field_in_factor_refused(armor):
    body = _v2_body(armor)
    body["factors"][0]["extra"] = "AAAA"
    with pytest.raises(ArmorError):
        parse_armor(_reseal(body))


def test_unknown_factor_type_refused(armor):
    body = _v2_body(armor)
    body["factors"][0]["type"] = "backdoor"
    with pytest.raises(ArmorError, match="unknown type|registry"):
        parse_armor(_reseal(body))


def test_duplicate_factor_type_refused(armor):
    body = _v2_body(armor)
    body["factors"].append(dict(body["factors"][0]))
    with pytest.raises(ArmorError, match="duplicate factor type"):
        parse_armor(_reseal(body))


def test_empty_factor_list_refused(armor):
    body = _v2_body(armor)
    body["factors"] = []
    with pytest.raises(ArmorError, match="non-empty"):
        parse_armor(_reseal(body))


def test_relabelled_root_pub_fails_aad(root, armor):
    # Rewriting root_pub to another key must fail AUTHENTICATION specifically —
    # the factor wrap's AAD binds root_pub, so with the right passphrase but a
    # relabelled root_pub the GCM tag fails: proves AAD-auth, not just "some
    # error". A blob cannot be re-labelled as another identity's key.
    body = _v2_body(armor)
    body["root_pub"] = KeyPair.generate().public_hex
    with pytest.raises(ArmorPassphraseError):
        decrypt_root_key(_reseal(body), _PW)


def test_factor_dispatch_is_total(root):
    # F4 (review finding): every registered factor type MUST have a strict
    # parser, so a type can never clear the membership check yet hit no
    # field-closure (which would reopen I1 at the growth point). The registry IS
    # the parser table — this guards against adding a type without its parser.
    from tools.network.idkit import armor as _armor

    assert set(_armor._KNOWN_FACTOR_TYPES) == set(_armor._FACTOR_PARSERS)
    assert all(callable(p) for p in _armor._FACTOR_PARSERS.values())


def test_factor_splice_across_armors_fails(root):
    # AEAD binding: a factor from one armor wraps THAT armor's master KEK, so
    # splicing it onto another armor's kek_seal yields the wrong KEK and the
    # seal fails to open. Material minted for one envelope can't be replayed.
    a = encrypt_root_key(root, _PW, iterations=10_000)
    b = encrypt_root_key(root, _PW, iterations=10_000)
    body_b = _v2_body(b)
    body_b["factors"] = _v2_body(a)["factors"]  # A's factor onto B's seal
    with pytest.raises((MalformedError, ArmorPassphraseError)):
        decrypt_root_key(_reseal(body_b), _PW)


def test_seal_tamper_fails(armor):
    # Flipping a byte of the master-KEK-sealed seed must fail the seal's GCM tag
    # (the password factor opens fine; the seal does not) -> MalformedError.
    body = _v2_body(armor)
    ct = bytearray(base64.b64decode(body["kek_seal"]["ct"]))
    ct[0] ^= 0xFF
    body["kek_seal"]["ct"] = base64.b64encode(bytes(ct)).decode()
    with pytest.raises(MalformedError):
        decrypt_root_key(_reseal(body), _PW)


def test_the_seal_and_factor_aads_are_distinct():
    """Domain separation between the two slots in one armor: a ciphertext
    minted to wrap the master KEK must not verify as the seal over the seed,
    or a factor could be spliced into the seal position."""
    from tools.network.idkit.armor import _v2_factor_aad, _v2_seal_aad

    rp = "ab" * 32
    factors = [{
        "type": "password",
        "kdf": {"name": "PBKDF2", "hash": "SHA-256", "iterations": 10_000,
                "salt": base64.b64encode(b"s" * 16).decode()},
        "cipher": "AES-256-GCM",
        "iv": base64.b64encode(b"i" * 12).decode(),
        "wrap": base64.b64encode(b"w" * 48).decode(),
    }]

    assert _v2_seal_aad(rp, factors) != _v2_factor_aad(rp, "password")


@pytest.mark.parametrize("field", ["iv", "wrap"])
def test_bad_factor_lengths_refused(armor, field):
    body = _v2_body(armor)
    body["factors"][0][field] = base64.b64encode(b"short").decode()
    with pytest.raises(ArmorError):
        parse_armor(_reseal(body))


def test_armor_root_pub_version_agnostic(root):
    v1 = encrypt_root_key(root, _PW, iterations=10_000)
    v2 = encrypt_root_key(root, _PW, iterations=10_000)
    from tools.network.idkit.armor import armor_root_pub
    assert armor_root_pub(v1) == root.public_hex
    assert armor_root_pub(v2) == root.public_hex
    with pytest.raises(ArmorError):
        armor_root_pub("not an armor")


# ── passkey factor (the root-armor half of a promotable passkey) ──────────
#
# A passkey factor seals the master KEK to a passkey's provisioning key — the
# public half the enrollment ceremony publishes. A fresh PRF eval re-derives
# the private half and opens the armor alone. These prove the round-trip and
# the guardrails; the browser mirror + a cross-impl vector make it real for the
# UI (that lands next, as the enrollment statement did).


def _passkey_pub(prf_output: bytes) -> str:
    """The public half a passkey publishes, from a PRF-output stand-in —
    derived exactly as the enrollment ceremony derives the provisioning key."""
    _, public_hex = derive_encapsulation_keypair(prf_output, PASSKEY_ARMOR_PURPOSE)
    return public_hex


def test_passkey_factor_opens_alone(root, armor):
    prf = b"\x11" * 32
    a = add_passkey_factor(armor, _PW, "cred-alpha", _passkey_pub(prf))
    assert armor_factor_types(a) == ["password", "passkey"]
    # both the password and the passkey open the SAME identity, independently
    assert decrypt_root_key(a, _PW).private_hex == root.private_hex
    assert decrypt_root_key_with_passkey(a, prf).private_hex == root.private_hex


def test_two_passkeys_open_independently(root, armor):
    prf1, prf2 = b"\x01" * 32, b"\x02" * 32
    a = add_passkey_factor(armor, _PW, "cred-1", _passkey_pub(prf1))
    a = add_passkey_factor(a, _PW, "cred-2", _passkey_pub(prf2))
    assert armor_factor_types(a).count("passkey") == 2
    assert decrypt_root_key_with_passkey(a, prf1).private_hex == root.private_hex
    assert decrypt_root_key_with_passkey(a, prf2).private_hex == root.private_hex


def test_wrong_prf_output_refused(armor):
    a = add_passkey_factor(armor, _PW, "cred-1", _passkey_pub(b"\x03" * 32))
    with pytest.raises(ArmorPassphraseError):
        decrypt_root_key_with_passkey(a, b"\x04" * 32)


def test_duplicate_credential_refused(armor):
    a = add_passkey_factor(armor, _PW, "cred-dup", _passkey_pub(b"\x05" * 32))
    with pytest.raises(ArmorError):
        add_passkey_factor(a, _PW, "cred-dup", _passkey_pub(b"\x06" * 32))


def test_remove_passkey_factor(root, armor):
    prf = b"\x07" * 32
    a = add_passkey_factor(armor, _PW, "cred-x", _passkey_pub(prf))
    a = remove_passkey_factor(a, _PW, "cred-x")
    assert armor_factor_types(a) == ["password"]
    assert decrypt_root_key(a, _PW).private_hex == root.private_hex
    with pytest.raises(ArmorPassphraseError):
        decrypt_root_key_with_passkey(a, prf)


def test_remove_unknown_credential_refused(armor):
    a = add_passkey_factor(armor, _PW, "cred-real", _passkey_pub(b"\x08" * 32))
    with pytest.raises(ArmorError):
        remove_passkey_factor(a, _PW, "cred-ghost")


def test_cannot_remove_the_last_factor(armor):
    # An armor whose only factor is one passkey (built by adding then dropping
    # the password) refuses to drop that last passkey.
    from tools.network.idkit.armor import remove_factor
    a = add_passkey_factor(armor, _PW, "cred-only", _passkey_pub(b"\x0a" * 32))
    a = remove_factor(a, _PW, "password")
    assert armor_factor_types(a) == ["passkey"]
    with pytest.raises(ArmorError):
        remove_passkey_factor(a, _PW, "cred-only")


def test_stripping_a_passkey_factor_fails_closed(armor):
    a = add_passkey_factor(armor, _PW, "cred-1", _passkey_pub(b"\x09" * 32))
    data = parse_armor(a)
    data["factors"] = [f for f in data["factors"] if f["type"] != "passkey"]
    body = base64.b64encode(
        json.dumps(data, separators=(",", ":"), sort_keys=True).encode()
    ).decode()
    tampered = "\n".join([ARMOR_BEGIN, body, ARMOR_END])
    # Fails closed: the seal commits to the exact factor set, so a stripped
    # factor mismatches the seal's AAD and the seed unseal fails its GCM tag.
    with pytest.raises((ArmorError, ArmorPassphraseError, MalformedError)):
        decrypt_root_key(tampered, _PW)
