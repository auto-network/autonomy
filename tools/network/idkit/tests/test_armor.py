"""Armor tests — the passphrase-encrypted org root key blob (I1)."""

import base64
import json

import pytest

from tools.network.idkit import KeyPair
from tools.network.idkit.errors import MalformedError
from tools.network.idkit.armor import (
    ARMOR_BEGIN,
    ARMOR_END,
    ArmorError,
    ArmorPassphraseError,
    armor_version,
    decrypt_root_key,
    decrypt_root_key_any,
    decrypt_root_key_v2,
    encrypt_root_key,
    encrypt_root_key_v2,
    migrate_v1_to_v2,
    parse_armor,
    parse_armor_v2,
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


# ── strict parsing: the smuggling channel is closed (Codex finding) ───
#
# parse_armor tolerated unknown decoded fields, so a decryptable armor
# could carry {private_hex: <seed>} in its body and ride into storage
# verbatim. Strict parsing rejects any non-canonical shape outright.


def _reencode(data: dict) -> str:
    return "\n".join(
        [ARMOR_BEGIN, base64.b64encode(json.dumps(data).encode()).decode(), ARMOR_END]
    )


def test_smuggled_field_rejected(root, armor):
    data = parse_armor(armor)
    data["private_hex"] = root.private_hex
    with pytest.raises(ArmorError, match="unknown fields|exactly"):
        parse_armor(_reencode(data))


@pytest.mark.parametrize("where", ["kdf", "cipher"])
def test_smuggled_nested_field_rejected(root, armor, where):
    data = parse_armor(armor)
    data[where]["private_hex"] = root.private_hex
    with pytest.raises(ArmorError):
        parse_armor(_reencode(data))


@pytest.mark.parametrize(
    "mangle",
    [
        lambda d: d.__setitem__("root_pub", d["root_pub"].upper()),
        lambda d: d.__setitem__("root_pub", d["root_pub"][:32]),
        lambda d: d["kdf"].__setitem__("salt", base64.b64encode(b"short").decode()),
        lambda d: d["cipher"].__setitem__("iv", base64.b64encode(b"x" * 16).decode()),
        lambda d: d.__setitem__("ct", base64.b64encode(b"x" * 47).decode()),
        lambda d: d.__setitem__("ct", "not*base64!"),
        lambda d: d.pop("ct"),
        lambda d: d["kdf"].pop("salt"),
    ],
)
def test_malformed_fields_rejected(armor, mangle):
    data = parse_armor(armor)
    mangle(data)
    with pytest.raises(ArmorError):
        parse_armor(_reencode(data))


def test_canonicalize_roundtrip(root, armor):
    from tools.network.idkit.armor import canonicalize_armor

    canonical = canonicalize_armor(armor)
    assert canonical == armor  # encrypt_root_key already emits canonical form
    opened = decrypt_root_key(canonical, "correct horse")
    assert opened.private_hex == root.private_hex


def test_canonicalize_refuses_smuggled_armor(root, armor):
    from tools.network.idkit.armor import canonicalize_armor

    data = parse_armor(armor)
    data["private_hex"] = root.private_hex
    with pytest.raises(ArmorError):
        canonicalize_armor(_reencode(data))


@pytest.mark.parametrize("dup_key", ["v", "ct", "root_pub"])
def test_duplicate_body_keys_rejected_at_parser(armor, dup_key):
    """json.loads is last-key-wins on duplicates — a clean-looking parse
    could hide a shadowed field. Rejected at the parser boundary."""
    data = parse_armor(armor)
    obj = json.dumps(data)
    assert obj.endswith("}")
    forged_json = obj[:-1] + f', "{dup_key}": {json.dumps(data[dup_key])}}}'
    forged = "\n".join(
        [ARMOR_BEGIN, base64.b64encode(forged_json.encode()).decode(), ARMOR_END]
    )
    with pytest.raises(ArmorError, match="duplicate"):
        parse_armor(forged)


# ── Armor v2: versioned multi-wrap envelope + one-shot v1->v2 migration ──────

_V2_PW = "correct horse battery staple"


@pytest.fixture(scope="module")
def armor_v2(root: KeyPair) -> str:
    return encrypt_root_key_v2(root, _V2_PW, iterations=10_000)


def test_v2_roundtrip(root, armor_v2):
    opened = decrypt_root_key_v2(armor_v2, _V2_PW)
    assert opened.private_hex == root.private_hex
    assert opened.public_hex == root.public_hex


def test_v2_shape_is_versioned_factor_list(root, armor_v2):
    data = parse_armor_v2(armor_v2)
    assert data["v"] == 2
    assert set(data) == {"v", "root_pub", "kek_seal", "factors"}
    assert data["root_pub"] == root.public_hex
    assert [f["type"] for f in data["factors"]] == ["password"]


def test_v2_never_contains_plaintext(root, armor_v2):
    assert root.private_hex not in armor_v2
    body = base64.b64decode(
        "".join(ln for ln in armor_v2.splitlines() if ln and "-----" not in ln)
    )
    assert bytes.fromhex(root.private_hex) not in body


def test_v2_wrong_passphrase(armor_v2):
    with pytest.raises(ArmorPassphraseError):
        decrypt_root_key_v2(armor_v2, "not the passphrase")


def test_migration_v1_to_v2_recovers_exact_key(root):
    v1 = encrypt_root_key(root, _V2_PW, iterations=10_000)
    assert armor_version(v1) == 1
    v2 = migrate_v1_to_v2(v1, _V2_PW, iterations=10_000)
    assert armor_version(v2) == 2
    opened = decrypt_root_key_v2(v2, _V2_PW)
    assert opened.private_hex == root.private_hex
    assert opened.public_hex == root.public_hex


def test_migration_refuses_a_v2_input(armor_v2):
    with pytest.raises(ArmorError):
        migrate_v1_to_v2(armor_v2, _V2_PW)


def test_version_dispatch_opens_both(root):
    v1 = encrypt_root_key(root, _V2_PW, iterations=10_000)
    v2 = encrypt_root_key_v2(root, _V2_PW, iterations=10_000)
    assert armor_version(v1) == 1 and armor_version(v2) == 2
    assert decrypt_root_key_any(v1, _V2_PW).private_hex == root.private_hex
    assert decrypt_root_key_any(v2, _V2_PW).private_hex == root.private_hex


def test_v1_parser_refuses_v2_and_vice_versa(armor, armor_v2):
    with pytest.raises(ArmorError):
        parse_armor(armor_v2)          # v1 strict parser must reject a v2 body
    with pytest.raises(ArmorError):
        parse_armor_v2(armor)          # v2 strict parser must reject a v1 body


def _v2_body(armor_v2: str) -> dict:
    lines = [ln for ln in armor_v2.strip().splitlines() if ln.strip()]
    return json.loads(base64.b64decode("".join(lines[1:-1])))


def _reseal(body: dict) -> str:
    b64 = base64.b64encode(json.dumps(body, separators=(",", ":")).encode()).decode()
    return "\n".join([ARMOR_BEGIN, b64, ARMOR_END])


def test_v2_extra_toplevel_field_refused_I1(armor_v2):
    body = _v2_body(armor_v2)
    body["smuggled"] = "AAAA"
    with pytest.raises(ArmorError, match="I1|unknown fields"):
        parse_armor_v2(_reseal(body))


def test_v2_extra_field_in_kek_seal_refused(armor_v2):
    body = _v2_body(armor_v2)
    body["kek_seal"]["extra"] = "AAAA"
    with pytest.raises(ArmorError):
        parse_armor_v2(_reseal(body))


def test_v2_extra_field_in_factor_refused(armor_v2):
    body = _v2_body(armor_v2)
    body["factors"][0]["extra"] = "AAAA"
    with pytest.raises(ArmorError):
        parse_armor_v2(_reseal(body))


def test_v2_unknown_factor_type_refused(armor_v2):
    body = _v2_body(armor_v2)
    body["factors"][0]["type"] = "backdoor"
    with pytest.raises(ArmorError, match="unknown type|registry"):
        parse_armor_v2(_reseal(body))


def test_v2_duplicate_factor_type_refused(armor_v2):
    body = _v2_body(armor_v2)
    body["factors"].append(dict(body["factors"][0]))
    with pytest.raises(ArmorError, match="duplicate factor type"):
        parse_armor_v2(_reseal(body))


def test_v2_empty_factor_list_refused(armor_v2):
    body = _v2_body(armor_v2)
    body["factors"] = []
    with pytest.raises(ArmorError, match="non-empty"):
        parse_armor_v2(_reseal(body))


def test_v2_relabelled_root_pub_fails_aad(root, armor_v2):
    # Rewriting root_pub to another key must fail AUTHENTICATION specifically —
    # the factor wrap's AAD binds root_pub, so with the right passphrase but a
    # relabelled root_pub the GCM tag fails: proves AAD-auth, not just "some
    # error". A blob cannot be re-labelled as another identity's key.
    body = _v2_body(armor_v2)
    body["root_pub"] = KeyPair.generate().public_hex
    with pytest.raises(ArmorPassphraseError):
        decrypt_root_key_v2(_reseal(body), _V2_PW)


def test_v2_factor_dispatch_is_total(root):
    # F4 (review finding): every registered factor type MUST have a strict
    # parser, so a type can never clear the membership check yet hit no
    # field-closure (which would reopen I1 at the growth point). The registry IS
    # the parser table — this guards against adding a type without its parser.
    from tools.network.idkit import armor as _armor

    assert set(_armor._KNOWN_FACTOR_TYPES) == set(_armor._FACTOR_PARSERS)
    assert all(callable(p) for p in _armor._FACTOR_PARSERS.values())


def test_v2_factor_splice_across_armors_fails(root):
    # AEAD binding: a factor from one armor wraps THAT armor's master KEK, so
    # splicing it onto another armor's kek_seal yields the wrong KEK and the
    # seal fails to open. Material minted for one envelope can't be replayed.
    a = encrypt_root_key_v2(root, _V2_PW, iterations=10_000)
    b = encrypt_root_key_v2(root, _V2_PW, iterations=10_000)
    body_b = _v2_body(b)
    body_b["factors"] = _v2_body(a)["factors"]  # A's factor onto B's seal
    with pytest.raises((MalformedError, ArmorPassphraseError)):
        decrypt_root_key_v2(_reseal(body_b), _V2_PW)


def test_v2_seal_tamper_fails(armor_v2):
    # Flipping a byte of the master-KEK-sealed seed must fail the seal's GCM tag
    # (the password factor opens fine; the seal does not) -> MalformedError.
    body = _v2_body(armor_v2)
    ct = bytearray(base64.b64decode(body["kek_seal"]["ct"]))
    ct[0] ^= 0xFF
    body["kek_seal"]["ct"] = base64.b64encode(bytes(ct)).decode()
    with pytest.raises(MalformedError):
        decrypt_root_key_v2(_reseal(body), _V2_PW)


def test_v2_aad_prefixes_are_distinct_across_slot_and_version():
    # Domain separation: the seal AAD, the factor AAD, and the v1 AAD are all
    # distinct, so no ciphertext minted for one slot/version verifies as another.
    from tools.network.idkit.armor import _aad, _v2_factor_aad, _v2_seal_aad

    rp = "ab" * 32
    assert _v2_seal_aad(rp) != _v2_factor_aad(rp, "password")
    assert _v2_seal_aad(rp) != _aad(rp)
    assert _v2_factor_aad(rp, "password") != _aad(rp)


@pytest.mark.parametrize("field", ["iv", "wrap"])
def test_v2_bad_factor_lengths_refused(armor_v2, field):
    body = _v2_body(armor_v2)
    body["factors"][0][field] = base64.b64encode(b"short").decode()
    with pytest.raises(ArmorError):
        parse_armor_v2(_reseal(body))


def test_armor_root_pub_version_agnostic(root):
    v1 = encrypt_root_key(root, _V2_PW, iterations=10_000)
    v2 = encrypt_root_key_v2(root, _V2_PW, iterations=10_000)
    from tools.network.idkit.armor import armor_root_pub
    assert armor_root_pub(v1) == root.public_hex
    assert armor_root_pub(v2) == root.public_hex
    with pytest.raises(ArmorError):
        armor_root_pub("not an armor")
