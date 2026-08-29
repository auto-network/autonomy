"""The armor facade: version-3 envelopes are served; everything else is refused.

The version-2 identity armor implementation is deleted; these tests pin the
facade's contract — classification and canonicalization of the one live
format, and a loud retired-format refusal for anything older."""

import base64

import pytest

from tools.network.idkit.armor import (
    ArmorError,
    armor_factor_types,
    armor_root_pub,
    armor_version,
    canonicalize_armor,
)
from tools.network.idkit.keys import KeyPair
from tools.network.idkit.root_factor_policy import mint_password_armor

ITERS = 10_000


@pytest.fixture(scope="module")
def armored():
    key = KeyPair.generate()
    return key, mint_password_armor(key, "hunter2", iterations=ITERS)


def test_v3_classifies(armored):
    _, armor = armored
    assert armor_version(armor) == 3


def test_v3_root_pub(armored):
    key, armor = armored
    assert armor_root_pub(armor) == key.public_hex


def test_v3_factor_types(armored):
    _, armor = armored
    assert armor_factor_types(armor) == ["password"]


def test_v3_canonicalizes_stably(armored):
    _, armor = armored
    once = canonicalize_armor(armor)
    assert canonicalize_armor(once) == once


def _fake_armor(body: bytes) -> str:
    b64 = base64.b64encode(body).decode()
    return (
        "-----BEGIN AUTONOMY NETWORK ROOT KEY-----\n"
        + b64
        + "\n-----END AUTONOMY NETWORK ROOT KEY-----"
    )


@pytest.mark.parametrize("body", [b'{"v": 2}', b'{"v": 1}', b'{"v": 4}'])
def test_retired_and_unknown_versions_are_refused(body):
    for probe in (armor_version, canonicalize_armor, armor_root_pub, armor_factor_types):
        with pytest.raises(ArmorError):
            probe(_fake_armor(body))


def test_garbage_is_refused():
    with pytest.raises(ArmorError):
        armor_version("not an armor at all")
