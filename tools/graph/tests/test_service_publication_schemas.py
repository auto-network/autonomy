"""Red-first contract for sovereign Service namespace reservations.

Design authority: graph://c880c5e6-8bd@3, sections 3.1-3.3.
Bead: auto-otkhu.
"""

from __future__ import annotations

import importlib

import pytest

from tools.graph import schemas
from tools.graph.schemas.registry import SchemaValidationError


SET_ID = "autonomy.network.namespace-reservation"
REVISION = 1
UUID_NAMESPACE = "6cf440db-c8b4-566c-99db-e7be17109bdc"


def _implementation():
    """Import inside each test so the red suite collects before code exists."""
    return importlib.import_module("tools.dashboard.service_publication")


def _schema_module():
    return importlib.import_module("tools.graph.schemas.namespace_reservation")


@pytest.mark.parametrize(
    ("display_name", "persona_pub", "app_label", "persona_label", "reservation_id"),
    [
        (
            "Jérëmy 未来",
            "00" * 32,
            "docs",
            "jeremy-66687aadf862bd776c8f",
            "d3a99356-6161-5507-81c5-71c89f7fdf57",
        ),
        (
            "未来",
            "ff" * 32,
            "port-8000",
            "persona-af9613760f72635fbdb4",
            "315d1b91-12d1-5a38-9936-a98c686ba973",
        ),
    ],
)
def test_published_hostname_and_uuid_vectors(
    display_name, persona_pub, app_label, persona_label, reservation_id
):
    impl = _implementation()

    assert impl.RESERVATION_UUID_NAMESPACE == UUID_NAMESPACE
    assert impl.normalize_persona_label(display_name, persona_pub) == persona_label
    assert impl.reservation_key(persona_pub, app_label) == reservation_id


@pytest.mark.parametrize("label", ["a", "docs", "port-8000", "a" * 63])
def test_app_labels_accept_only_the_exact_dns_vocabulary(label):
    assert _implementation().validate_app_label(label) == label


@pytest.mark.parametrize(
    "label",
    [
        "",
        "A",
        "has.dot",
        "has space",
        "-leading",
        "trailing-",
        "a" * 64,
        "_autonomy",
        "www",
        "api",
        "relay",
        "registry",
        "auto",
        "serve",
    ],
)
def test_app_labels_reject_malformed_and_reserved_values(label):
    with pytest.raises(ValueError):
        _implementation().validate_app_label(label)


def test_schema_registration_declares_identity_home_and_band():
    module = _schema_module()

    cls = schemas.get_schema(SET_ID, REVISION)
    assert cls is module.NamespaceReservationV1
    assert schemas.declared_home(SET_ID) == "organization"
    assert schemas.declared_band(SET_ID, REVISION) == ("raw", "raw")
    assert cls._access_pattern == "keyed_per_entity"
    assert cls._key_strategy == "reservation_id"


def _payload(**changes):
    value = {
        "persona_pub": "00" * 32,
        "persona_label": "jeremy-66687aadf862bd776c8f",
        "app_label": "docs",
        "state": "active",
        "created_at": "2026-08-30T05:35:38.123Z",
        "updated_at": "2026-08-30T05:35:38.123Z",
    }
    value.update(changes)
    return value


def test_exact_payload_accepts_optional_release_and_product_reference():
    _schema_module()

    schemas.validate_payload(SET_ID, REVISION, _payload())
    schemas.validate_payload(
        SET_ID,
        REVISION,
        _payload(
            state="released",
            released_at="2026-08-30T06:00:00.000Z",
            product_ref="package:personal-autonomy",
        ),
    )


@pytest.mark.parametrize("forbidden", ["reservation_id", "org", "organization", "origin"])
def test_payload_never_repeats_key_org_or_derived_origin(forbidden):
    _schema_module()

    with pytest.raises(SchemaValidationError):
        schemas.validate_payload(SET_ID, REVISION, _payload(**{forbidden: "forbidden"}))


@pytest.mark.parametrize(
    "changes",
    [
        {"persona_pub": "00" * 31},
        {"persona_label": "Uppercase"},
        {"app_label": "www"},
        {"state": "unknown"},
        {"created_at": "2026-08-30T05:35:38Z"},
        {"created_at": "2026-99-99T99:99:99.999Z"},
        {"updated_at": "2026-08-30 05:35:38.123Z"},
        {"updated_at": "2026-02-30T05:35:38.123Z"},
        {"state": "active", "released_at": "2026-08-30T06:00:00.000Z"},
        {"state": "released"},
        {"product_ref": ""},
        {"product_ref": "x" * 129},
        {"product_ref": "not\nprintable"},
    ],
)
def test_payload_validation_is_byte_exact(changes):
    _schema_module()

    with pytest.raises(SchemaValidationError):
        schemas.validate_payload(SET_ID, REVISION, _payload(**changes))


def test_member_key_is_a_canonical_lowercase_uuid():
    _schema_module()
    good = "d3a99356-6161-5507-81c5-71c89f7fdf57"

    schemas.validate_key(SET_ID, REVISION, good)
    for bad in (good.upper(), "{" + good + "}", good.replace("-", ""), "not-a-uuid"):
        with pytest.raises(SchemaValidationError):
            schemas.validate_key(SET_ID, REVISION, bad)
