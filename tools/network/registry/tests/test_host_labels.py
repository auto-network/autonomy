"""Host registration refuses RFC 5891 tagged labels (`xn--`): a Punycode label
passes every ASCII look-alike check and displays as another script's name."""

from __future__ import annotations

import uuid

import pytest

from tools.network.registry import relay


PERSONA = "ab" * 32


def _reservation(app_label: str) -> str:
    return str(uuid.uuid5(relay.RESERVATION_NAMESPACE, f"{PERSONA}\0{app_label}"))


def _persona_label(slug: str) -> str:
    return f"{slug}-{relay.persona_label_suffix(PERSONA)}"


def test_a_plain_host_registers():
    host = f"docs.{_persona_label('jeremy')}.{relay.SERVE_BASE_DOMAIN}"
    assert relay.validate_host_registration(host, _reservation("docs"), PERSONA) == (
        "docs", _persona_label("jeremy"))


@pytest.mark.parametrize("app_label", ["xn--80ak6aa92e", "ab--cd"])
def test_tagged_app_labels_are_refused(app_label):
    host = f"{app_label}.{_persona_label('jeremy')}.{relay.SERVE_BASE_DOMAIN}"
    with pytest.raises(relay.HostValidationError, match="reserved or malformed"):
        relay.validate_host_registration(host, _reservation(app_label), PERSONA)


def test_tagged_persona_labels_are_refused():
    host = f"docs.{_persona_label('xn--80ak6aa92e')}.{relay.SERVE_BASE_DOMAIN}"
    with pytest.raises(relay.HostValidationError, match="persona label is malformed"):
        relay.validate_host_registration(host, _reservation("docs"), PERSONA)
