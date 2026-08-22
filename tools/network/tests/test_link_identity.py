import pytest

from tools.network.link_identity import (
    LinkBinding,
    LinkBindingKind,
    commitment_fragment,
    verify_fragment,
)


def test_identity_pin_cross_language_vector():
    binding = LinkBinding(LinkBindingKind.PERSONAL, "00" * 32)
    salt = bytes(range(16))
    assert commitment_fragment(binding, salt) == "ac=AO8Za0AURuoJo-fno_p-Nw"
    assert verify_fragment("ac=AO8Za0AURuoJo-fno_p-Nw", binding, salt)


def test_identity_pin_binds_kind_and_genesis():
    salt = bytes(range(16))
    personal = LinkBinding(LinkBindingKind.PERSONAL, "00" * 32)
    persona = LinkBinding(LinkBindingKind.PERSONA, "00" * 32)
    assert commitment_fragment(personal, salt) != commitment_fragment(persona, salt)
    assert not verify_fragment(commitment_fragment(personal, salt), persona, salt)


def test_identity_pin_rejects_bad_fragment_and_salt():
    binding = LinkBinding(LinkBindingKind.ORGANIZATION, "11" * 32)
    with pytest.raises(ValueError):
        commitment_fragment(binding, b"short")
    assert not verify_fragment("ac=bad", binding, bytes(16))
