"""``autonomy.org`` schema revisions 1/2/3 and the icon-field bounds.

Revision 3 (auto-j1y0z) adds the portable organization icon: an
``icon_attachment_id`` (canonical 512x512 WebP in the org attachment store)
and an ``icon_data_uri`` (bounded 64x64 WebP data: URI). A revision-1 or
revision-2 row upconverts without data loss, and the current-revision constant
must never relabel an older class.
"""
from __future__ import annotations

import pytest

from tools.graph.schemas import org as org_schema
from tools.graph.schemas.registry import (
    SchemaValidationError,
    get_schema,
    upconvert_payload,
    validate_payload,
)

SET_ID = "autonomy.org"


class TestRevisionClasses:
    def test_current_revision_is_three(self):
        assert org_schema.ORG_REVISION == 3

    def test_each_class_keeps_its_own_revision(self):
        # The current-revision constant must never relabel an older class.
        assert org_schema.OrgV1.schema_revision == 1
        assert org_schema.OrgV2.schema_revision == 2
        assert org_schema.OrgV3.schema_revision == 3
        assert org_schema.OrgV3.schema_revision == org_schema.ORG_REVISION

    def test_registry_maps_each_revision_to_its_class(self):
        assert get_schema(SET_ID, 1) is org_schema.OrgV1
        assert get_schema(SET_ID, 2) is org_schema.OrgV2
        assert get_schema(SET_ID, 3) is org_schema.OrgV3


class TestUpconvert:
    def test_v1_to_v3_preserves_legacy_favicon(self):
        p1 = {"name": "Acme", "favicon": "/static/icon-192.png", "color": "#123456"}
        out = upconvert_payload(SET_ID, 1, 3, p1)
        assert out == p1
        # No icon fields are invented during upconversion.
        assert "icon_attachment_id" not in out
        assert "icon_data_uri" not in out

    def test_v2_to_v3_preserves_description_and_favicon(self):
        p2 = {
            "name": "Acme",
            "description": "long charter text",
            "favicon": "assets/acme.png",
        }
        out = upconvert_payload(SET_ID, 2, 3, p2)
        assert out == p2

    def test_v1_to_v2_still_works(self):
        p1 = {"name": "Acme", "byline": "hi"}
        assert upconvert_payload(SET_ID, 1, 2, p1) == p1

    def test_identity_upconvert_is_empty_chain(self):
        assert upconvert_payload(SET_ID, 3, 3, {"name": "Acme"}) == {"name": "Acme"}


class TestRevision3Validation:
    def test_accepts_both_icon_fields(self):
        validate_payload(SET_ID, 3, {
            "name": "Acme",
            "icon_attachment_id": "7c0c8c82-1234",
            "icon_data_uri": "data:image/webp;base64,AAAA",
        })

    def test_accepts_bare_name(self):
        validate_payload(SET_ID, 3, {"name": "Acme"})

    def test_data_uri_at_the_bound_is_accepted(self):
        uri = "d" * org_schema.ORG_ICON_DATA_URI_MAX_CHARS
        validate_payload(SET_ID, 3, {"name": "Acme", "icon_data_uri": uri})

    def test_data_uri_over_the_bound_is_rejected(self):
        uri = "d" * (org_schema.ORG_ICON_DATA_URI_MAX_CHARS + 1)
        with pytest.raises(SchemaValidationError) as exc:
            validate_payload(SET_ID, 3, {"name": "Acme", "icon_data_uri": uri})
        assert str(org_schema.ORG_ICON_DATA_URI_MAX_CHARS) in str(exc.value)

    def test_empty_icon_field_is_rejected(self):
        with pytest.raises(SchemaValidationError):
            validate_payload(SET_ID, 3, {"name": "Acme", "icon_attachment_id": ""})

    def test_non_string_icon_field_is_rejected(self):
        with pytest.raises(SchemaValidationError):
            validate_payload(SET_ID, 3, {"name": "Acme", "icon_data_uri": 123})

    def test_name_still_required(self):
        with pytest.raises(SchemaValidationError):
            validate_payload(SET_ID, 3, {"icon_attachment_id": "abc"})


class TestOlderRevisionsRejectIconFields:
    def test_v1_rejects_icon_attachment_id(self):
        with pytest.raises(SchemaValidationError):
            validate_payload(SET_ID, 1, {"name": "Acme", "icon_attachment_id": "x"})

    def test_v2_rejects_icon_data_uri(self):
        with pytest.raises(SchemaValidationError):
            validate_payload(SET_ID, 2, {"name": "Acme", "icon_data_uri": "data:x"})
