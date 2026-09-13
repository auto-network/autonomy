"""``autonomy.user#2`` — the mutable Personal profile schema (auto-vlt7j.1;
revision 2 per the 2026-09-13 ruling: the photo is the attachment).

Pins the exact field set and every bound of ``UserProfileV2``: the singleton
``default`` personal-homed ``raw`` contract that validates only display name,
biography, an initials override, the server-owned avatar attachment id, and
an update time. Reject unknown fields, malformed UUID/timestamp values,
leading/trailing whitespace, and invalid lengths. Revision 1 stays frozen
with its inline icon, and its upconverter drops that icon.
"""
from __future__ import annotations

import base64

import pytest

from tools.graph.schemas import user as user_schema
from tools.graph.schemas.registry import (
    SchemaValidationError,
    declared_band,
    declared_home,
    get_schema,
    states_allowed,
    validate_payload,
)

SET_ID = "autonomy.user"
REV = 2
REV1 = 1
TS = "2026-09-12T00:00:00Z"
# A canonical UUID (v7, as the attachment store mints them).
UUID = "0192a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b"


def _webp_data_uri(nbytes: int = 32) -> str:
    return user_schema.AVATAR_ICON_DATA_URI_PREFIX + base64.b64encode(
        b"R" * nbytes
    ).decode()


def _ok(**extra) -> dict:
    payload = {"display_name": "Jeremy Spilman", "updated_at": TS}
    payload.update(extra)
    return payload


class TestRegistrationAndContract:
    def test_registered_class_and_constants(self):
        assert get_schema(SET_ID, REV) is user_schema.UserProfileV2
        assert get_schema(SET_ID, REV1) is user_schema.UserProfileV1
        assert user_schema.USER_PROFILE_SET_ID == SET_ID
        assert user_schema.USER_PROFILE_REVISION == REV
        assert user_schema.USER_PROFILE_CANONICAL_LABEL == "default"

    def test_personal_homed_raw_singleton(self):
        cls = user_schema.UserProfileV1
        assert declared_home(SET_ID) == "personal"
        assert declared_band(SET_ID, REV) == ("raw", "raw")
        assert states_allowed(SET_ID, REV) == ("raw",)
        assert cls._access_pattern == "singleton"
        assert cls._key_strategy == "fixed:default"

    def test_exact_field_set(self):
        props = user_schema.UserProfileV2.export_json_schema()["properties"]
        assert set(props) == {
            "display_name", "biography", "initials",
            "avatar_attachment_id", "updated_at",
        }
        required = set(user_schema.UserProfileV2.export_json_schema()["required"])
        assert required == {"display_name", "updated_at"}

    def test_revision_1_is_frozen_with_its_inline_icon(self):
        props = user_schema.UserProfileV1.export_json_schema()["properties"]
        assert "avatar_icon_data_uri" in props
        validate_payload(SET_ID, REV1, _ok(avatar_icon_data_uri=_webp_data_uri()))

    def test_revision_2_refuses_the_inline_icon(self):
        with pytest.raises(SchemaValidationError):
            validate_payload(SET_ID, REV, _ok(avatar_icon_data_uri=_webp_data_uri()))

    def test_upconvert_drops_the_inline_icon(self):
        from tools.graph.schemas.registry import upconvert_chain
        [hop] = upconvert_chain(SET_ID, REV1, REV)
        out = hop(_ok(avatar_attachment_id=UUID, avatar_icon_data_uri=_webp_data_uri()))
        assert out == _ok(avatar_attachment_id=UUID)


class TestAccepts:
    def test_minimal(self):
        validate_payload(SET_ID, REV, _ok())

    def test_all_optional_fields(self):
        validate_payload(SET_ID, REV, _ok(
            biography="Builder of Autonomy.",
            initials="JS",
            avatar_attachment_id=UUID,
        ))

    def test_empty_biography_is_a_valid_cleared_value(self):
        validate_payload(SET_ID, REV, _ok(biography=""))

    def test_boundary_lengths(self):
        validate_payload(SET_ID, REV, _ok(display_name="x" * 120))
        validate_payload(SET_ID, REV, _ok(biography="x" * 500))
        validate_payload(SET_ID, REV, _ok(initials="x"))
        validate_payload(SET_ID, REV, _ok(initials="wxyz"))


class TestRejects:
    def test_missing_display_name(self):
        with pytest.raises(SchemaValidationError):
            validate_payload(SET_ID, REV, {"updated_at": TS})

    def test_missing_updated_at(self):
        with pytest.raises(SchemaValidationError):
            validate_payload(SET_ID, REV, {"display_name": "J"})

    def test_unknown_field(self):
        with pytest.raises(SchemaValidationError):
            validate_payload(SET_ID, REV, _ok(color="#123456"))

    @pytest.mark.parametrize("name", ["display_name", "biography", "initials"])
    def test_leading_trailing_whitespace(self, name):
        with pytest.raises(SchemaValidationError):
            validate_payload(SET_ID, REV, _ok(**{name: " padded "}))

    def test_display_name_empty(self):
        with pytest.raises(SchemaValidationError):
            validate_payload(SET_ID, REV, _ok(display_name=""))

    def test_display_name_too_long(self):
        with pytest.raises(SchemaValidationError):
            validate_payload(SET_ID, REV, _ok(display_name="x" * 121))

    def test_biography_too_long(self):
        with pytest.raises(SchemaValidationError):
            validate_payload(SET_ID, REV, _ok(biography="x" * 501))

    def test_initials_empty_override_never_persisted(self):
        # An empty initials value means "derive"; it is never a stored override.
        with pytest.raises(SchemaValidationError):
            validate_payload(SET_ID, REV, _ok(initials=""))

    def test_initials_too_long(self):
        with pytest.raises(SchemaValidationError):
            validate_payload(SET_ID, REV, _ok(initials="abcde"))

    def test_non_string_fields(self):
        for name in ("display_name", "biography", "initials",
                     "avatar_attachment_id", "updated_at"):
            with pytest.raises(SchemaValidationError):
                validate_payload(SET_ID, REV, _ok(**{name: 5}))

    def test_malformed_uuid(self):
        with pytest.raises(SchemaValidationError):
            validate_payload(SET_ID, REV, _ok(avatar_attachment_id="not-a-uuid"))

    def test_noncanonical_uuid(self):
        # Uppercase / braces are not the canonical lowercase form.
        with pytest.raises(SchemaValidationError):
            validate_payload(SET_ID, REV, _ok(avatar_attachment_id=UUID.upper()))
        with pytest.raises(SchemaValidationError):
            validate_payload(SET_ID, REV, _ok(avatar_attachment_id="{" + UUID + "}"))

    def test_malformed_data_uri_at_revision_1(self):
        # Wrong mime, non-base64 payload, and empty payload each fail.
        with pytest.raises(SchemaValidationError):
            validate_payload(SET_ID, REV1, _ok(
                avatar_icon_data_uri="data:image/png;base64,AAAA"))
        with pytest.raises(SchemaValidationError):
            validate_payload(SET_ID, REV1, _ok(
                avatar_icon_data_uri=user_schema.AVATAR_ICON_DATA_URI_PREFIX + "!!!!"))
        with pytest.raises(SchemaValidationError):
            validate_payload(SET_ID, REV1, _ok(
                avatar_icon_data_uri=user_schema.AVATAR_ICON_DATA_URI_PREFIX))

    def test_oversized_data_uri_at_revision_1(self):
        big = user_schema.AVATAR_ICON_DATA_URI_PREFIX + "A" * (
            user_schema.AVATAR_ICON_DATA_URI_MAX_CHARS + 1)
        with pytest.raises(SchemaValidationError):
            validate_payload(SET_ID, REV1, _ok(avatar_icon_data_uri=big))

    def test_malformed_timestamp(self):
        for bad in ("2026-09-12", "2026-09-12T00:00:00", "not-a-ts",
                    "2026-09-12 00:00:00Z"):
            with pytest.raises(SchemaValidationError):
                validate_payload(SET_ID, REV, _ok(updated_at=bad))
